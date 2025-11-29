import dspy
from typing import TypedDict, List, Annotated, Literal, Any
from langgraph.graph import StateGraph, END
import operator

# Import your components
from agent.rag.retrieval import LocalRetriever
from agent.tools.sqlite_tool import SQLiteTool
from agent.dspy_signatures import Router, TextToSQL, HybridSynthesizer

# --- 0. Configuration & Setup ---

# Configure DSPy with strict settings
lm = dspy.LM(
    model="ollama/phi3.5:3.8b-mini-instruct-q4_K_M", 
    api_base="http://localhost:11434",
    temperature=0.0,
    num_predict=1000, 
    num_ctx=8192
)

dspy.configure(lm=lm)

# Initialize Tools
retriever = LocalRetriever()
sql_tool = SQLiteTool()

# --- 1. Define Agent State ---
class AgentState(TypedDict):
    question: str
    router_decision: str  # 'sql', 'rag', 'hybrid'
    
    # RAG Data
    retrieved_docs: List[dict]
    
    # SQL Data
    sql_query: str
    sql_result: List[dict] | str
    sql_error: str | None
    
    # Logic & Output
    retry_count: int
    final_answer: Any
    explanation: str
    citations: List[str]

# --- 2. Define DSPy Modules ---
router_module = dspy.Predict(Router)

# LOAD OPTIMIZED MODULE IF EXISTS
try:
    sql_generator = dspy.ChainOfThought(TextToSQL)
    sql_generator.load("agent/optimized_sql_module.json")
    print("🧠 Loaded Optimized SQL Module!")
except Exception as e:
    print(f"⚠️ Could not load optimized module, using default. Error: {e}")
    sql_generator = dspy.ChainOfThought(TextToSQL)

synthesizer = dspy.ChainOfThought(HybridSynthesizer)

# --- 3. Define Graph Nodes ---

def router_node(state: AgentState):
    """Decides if we need RAG, SQL, or Both."""
    print(f"--- ROUTER: Analyzing '{state['question']}' ---")
    try:
        pred = router_module(question=state['question'])
        decision = pred.classification.lower().strip()
    except Exception as e:
        print(f"⚠️ Router Error: {e}. Defaulting to 'hybrid'")
        decision = 'hybrid'

    decision = decision.replace(".", "").replace("'", "")
    
    if decision not in ['sql', 'rag', 'hybrid']:
        decision = 'hybrid'
    
    return {"router_decision": decision}

def retriever_node(state: AgentState):
    """Fetches relevant docs using BM25."""
    print("--- RETRIEVER: Searching docs ---")
    docs = retriever.retrieve(state['question'], top_k=3)
    return {"retrieved_docs": docs}

def planner_node(state: AgentState):
    """Placeholder planner node."""
    print("--- PLANNER: Analyzing constraints ---")
    return {}

def sql_generation_node(state: AgentState):
    """Generates SQL using DSPy."""
    current_retries = state.get('retry_count', 0)
    print(f"--- SQL GEN (Attempt {current_retries + 1}) ---")
    
    schema_context = sql_tool.get_schema()
    
    # 1. START CONTEXT with the Question
    question_ctx = f"Question: {state['question']}\n"
    
    # 2. INJECT RAG CONTEXT (The Missing Link!)
    # If we have docs, the SQL generator needs to know definitions (dates, formulas)
    if state.get('retrieved_docs'):
        question_ctx += "\nContext from Documents (Use these dates/definitions):"
        for d in state['retrieved_docs']:
            question_ctx += f"\n- {d['content']}"
    
    # 3. INJECT PREVIOUS ERROR (Repair Loop)
    if state.get('sql_error'):
        question_ctx += f"\n\nERROR IN PREVIOUS QUERY: {state['sql_error']}\nFix the syntax."

    try:
        # Uses 'db_schema' to match signature
        pred = sql_generator(question=question_ctx, db_schema=schema_context)
        clean_sql = pred.sql_query.replace("```sql", "").replace("```", "").strip()
    except Exception as e:
        print(f"⚠️ SQL Gen Error: {e}")
        clean_sql = "SELECT 1"
        
    return {"sql_query": clean_sql}

def sql_executor_node(state: AgentState):
    """Runs the SQL and captures results or errors."""
    print("--- EXECUTOR: Running Query ---")
    query = state['sql_query']
    result = sql_tool.execute_query(query)
    current_retries = state.get('retry_count', 0)
    
    if isinstance(result, str) and (result.startswith("Error") or result.startswith("SQL Error")):
        print(f"   ❌ Failed: {result}")
        # Increment retry count on failure
        return {"sql_result": None, "sql_error": result, "retry_count": current_retries + 1}
    else:
        print(f"   ✅ Success: {len(result)} rows")
        return {"sql_result": result, "sql_error": None}

def synthesizer_node(state: AgentState):
    """Combines everything into the final answer with proper type conversion."""
    print("--- SYNTHESIZER: Formatting Answer ---")
    
    from agent.output_parser import parse_final_answer, extract_format_hint_from_question
    import json
    
    doc_context = ""
    citations = []
    
    # Gather doc citations
    if state.get('retrieved_docs'):
        for d in state['retrieved_docs']:
            doc_context += f"[Source: {d['id']}] {d['content']}\n"
            citations.append(d['id'])
            
    sql_ctx = state.get('sql_query', "N/A")
    res_ctx = str(state.get('sql_result', "No data"))
    
    # Extract format hint from question
    format_hint = state.get('format_hint') or extract_format_hint_from_question(state['question'])
    print(f"   Expected format: {format_hint}")

    # Call DSPy synthesizer
    try:
        pred = synthesizer(
            question=state['question'],
            context=doc_context,
            sql_query=sql_ctx,
            sql_result=res_ctx,
            format_hint=format_hint
        )
    except Exception as e:
        print(f"   Warning: Synthesizer error: {e}")
        pred = type('obj', (object,), {
            'final_answer': 'Error',
            'explanation': str(e),
            'citations': []
        })
    
    # CRITICAL: Parse the LLM output into correct format
    parsed_answer = parse_final_answer(
        raw_answer=pred.final_answer,
        format_hint=format_hint,
        sql_result=state.get('sql_result')
    )
    
    print(f"   Raw answer: {pred.final_answer}")
    print(f"   Parsed answer: {parsed_answer} (type: {type(parsed_answer).__name__})")
    
    # Auto-detect SQL table citations
    if state.get('sql_query'):
        sql_lower = state['sql_query'].lower()
        
        # Map to exact names from assignment
        if 'from orders' in sql_lower or 'join orders' in sql_lower:
            citations.append('Orders')
        if 'order_items' in sql_lower or '"order details"' in sql_lower:
            citations.append('Order Details')
        if 'from products' in sql_lower or 'join products' in sql_lower:
            citations.append('Products')
        if 'from customers' in sql_lower or 'join customers' in sql_lower:
            citations.append('Customers')
        if 'categories' in sql_lower:
            citations.append('Categories')
    
    # Add LLM-provided citations (clean them up)
    try:
        if pred.citations:
            if isinstance(pred.citations, str):
                # Handle string representation of list like "['orders', 'products']"
                if pred.citations.startswith('['):
                    try:
                        parsed = json.loads(pred.citations.replace("'", '"'))
                        citations.extend([str(c) for c in parsed])
                    except:
                        citations.append(pred.citations)
                else:
                    citations.append(pred.citations)
            elif isinstance(pred.citations, list):
                for c in pred.citations:
                    if isinstance(c, str):
                        # Clean nested list strings
                        if c.startswith('['):
                            try:
                                nested = json.loads(c.replace("'", '"'))
                                citations.extend([str(x) for x in nested])
                            except:
                                citations.append(c)
                        else:
                            citations.append(c)
    except:
        pass
    
    # Deduplicate while preserving order
    seen = set()
    unique_citations = []
    for c in citations:
        if c not in seen:
            seen.add(c)
            unique_citations.append(c)
    
    # Truncate explanation to ~2 sentences
    explanation = pred.explanation if hasattr(pred, 'explanation') else ""
    if explanation:
        sentences = explanation.split('. ')
        explanation = '. '.join(sentences[:2])
        if not explanation.endswith('.'):
            explanation += '.'
    
    return {
        "final_answer": parsed_answer,  # NOW PROPERLY TYPED!
        "explanation": explanation[:300],
        "citations": unique_citations
    }
# --- 4. Define Edges & Graph ---

def should_repair(state: AgentState):
    """Conditional Edge: Decide to retry SQL or move on."""
    error = state.get('sql_error')
    retries = state.get('retry_count', 0)
    
    if error and retries < 2:
        return "retry"
    return "synthesize"

def router_edge(state: AgentState):
    """Conditional Edge: Route based on classification."""
    return state['router_decision']

def post_retrieval_edge(state: AgentState):
    """If Hybrid, go to Planner after retrieval. Else Synthesize."""
    if state['router_decision'] == 'hybrid':
        return "planner"
    return "synthesizer"

workflow = StateGraph(AgentState)

# Add Nodes
workflow.add_node("router", router_node)
workflow.add_node("retriever", retriever_node)
workflow.add_node("planner", planner_node)
workflow.add_node("sql_gen", sql_generation_node)
workflow.add_node("executor", sql_executor_node)
workflow.add_node("synthesizer", synthesizer_node)

# Add Edges
workflow.set_entry_point("router")

workflow.add_conditional_edges(
    "router",
    router_edge,
    {
        "rag": "retriever",
        "sql": "planner",
        "hybrid": "retriever" 
    }
)

workflow.add_conditional_edges(
    "retriever",
    post_retrieval_edge,
    {
        "planner": "planner",
        "synthesizer": "synthesizer"
    }
)

workflow.add_edge("planner", "sql_gen")
workflow.add_edge("sql_gen", "executor")

workflow.add_conditional_edges(
    "executor",
    should_repair,
    {
        "retry": "sql_gen",
        "synthesize": "synthesizer"
    }
)

workflow.add_edge("synthesizer", END)

app = workflow.compile()