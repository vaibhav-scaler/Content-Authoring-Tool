import json
from typing import List, Dict, Any

def get_mock_outline(
    class_title: str,
    duration_mins: int = 90,
    agendas: List[str] = None,
    rethink_feedback: str = None,
    additional_resources: str = None
) -> List[Dict[str, Any]]:
    """Generates an outline deck dynamically adapted to class_title, agendas, additional info, and rethink feedback."""
    from agents.memory_engine import _infer_domain_anchor_from_title
    
    agendas = [a for a in (agendas or []) if a and str(a).strip()]
    anchor_system = _infer_domain_anchor_from_title(class_title)

    # Dynamic base templates derived from class title and domain
    is_rethink = bool(rethink_feedback and rethink_feedback.strip())
    
    # Adapt title & pedagogy depending on whether rethink feedback was given
    if is_rethink:
        approach_prefix = "Alternative Approach: "
        analogy_title = f"Reframed Analogy: {anchor_system} under Stress"
    else:
        approach_prefix = ""
        analogy_title = f"Everyday Analogy: {anchor_system}"

    master_phases = [
        {"phase": "Instructor Note", "title": f"Instructor Note: {class_title} Setup", "concept": f"Pacing, terminal setup, and pedagogy for {class_title}", "visual": "none", "weight": 1.0},
        {"phase": "Agenda", "title": f"{approach_prefix}Roadmap & Real-World Scope", "concept": "; ".join(agendas[:3]) if agendas else f"Core roadmap for {class_title}", "visual": "mermaid", "weight": 0.8},
        {"phase": "Everyday Analogy", "title": analogy_title, "concept": f"Grounded real-world daily life physical model for {class_title}", "visual": "mermaid", "weight": 1.1},
        {"phase": "Why/What", "title": f"Core Problem in {class_title} & Naive Failure", "concept": "Quantified bottleneck, concurrency collision, and complexity trap", "visual": "mermaid", "weight": 1.1},
        {"phase": "Quiz", "title": "Checkpoint Quiz 1: Diagnosing the Bottleneck", "concept": "Active comprehension check with distractor analysis", "visual": "none", "weight": 0.7},
        {"phase": "Concept", "title": f"Core Primitives & State Guarantees: {class_title}", "concept": agendas[0] if agendas else "Internal data structures and invariant guarantees", "visual": "mermaid", "weight": 1.2},
        {"phase": "Code Along", "title": f"Code Walkthrough 1: Implementing Baseline {class_title}", "concept": "Writing clean baseline code with error boundaries", "visual": "none", "weight": 1.2},
        {"phase": "Failure Mode", "title": f"Production Failure Mode: High-Load Stress Case", "concept": "Stacktrace reproduction, packet/query timeout, and boundary leak", "visual": "mermaid", "weight": 1.0},
        {"phase": "Concept", "title": f"Scalability & Resilient Architecture: {class_title}", "concept": agendas[1] if len(agendas) > 1 else "Horizontal scaling, isolation partitions, and throughput", "visual": "image", "weight": 1.2},
        {"phase": "Quiz", "title": "Checkpoint Quiz 2: Architectural Trade-offs", "concept": "Distractor analysis evaluating latency vs consistency trade-offs", "visual": "none", "weight": 0.7},
        {"phase": "Code Along", "title": f"Code Walkthrough 2: Resilient Production Pattern", "concept": "Idempotent handler, exponential backoff, and circuit breaker", "visual": "none", "weight": 1.2},
        {"phase": "Tracing", "title": f"Interactive System Trace: Simulating Outage", "concept": "Step-by-step trace through service degradation and self-healing", "visual": "mermaid", "weight": 1.1},
        {"phase": "Concept", "title": f"Production Traps & Anti-Patterns: {class_title}", "concept": "Three subtle bugs junior engineers deploy to production", "visual": "mermaid", "weight": 1.0},
        {"phase": "FAQ", "title": "Common Misconceptions & High-Frequency Doubts", "concept": "Clarifying edge conditions and student doubts pre-emptively", "visual": "none", "weight": 0.9},
        {"phase": "Recap", "title": f"Master Synthesis: {class_title} Architecture", "concept": f"Unified mental model cementing {class_title}", "visual": "mermaid", "weight": 0.9}
    ]

    # Incorporate custom agendas if more are provided
    if len(agendas) > 2:
        # replace generic concept cards with agenda items
        for idx, ag in enumerate(agendas[2:], start=8):
            if idx < len(master_phases):
                master_phases[idx]["concept"] = f"Focused breakdown of: {ag}"
                master_phases[idx]["title"] = f"Deep Dive: {ag[:40]}"

    # Dynamically determine target card count based on duration (roughly 5-6 mins per card)
    if duration_mins <= 30:
        target_count = 6
    elif duration_mins <= 45:
        target_count = 8
    elif duration_mins <= 60:
        target_count = 10
    elif duration_mins <= 90:
        target_count = 14
    elif duration_mins <= 120:
        target_count = 16
    else:
        target_count = min(len(master_phases), max(6, int(duration_mins / 6.0)))

    selected = master_phases[:target_count]
    total_weight = sum(item["weight"] for item in selected)

    # Distribute duration proportionally, rounded to 1 decimal place
    allocated = []
    current_sum = 0.0
    for idx, item in enumerate(selected):
        raw_dur = round((item["weight"] / total_weight) * duration_mins, 1)
        # Avoid zero or sub-2 minute cards
        raw_dur = max(2.5, raw_dur)
        allocated.append(raw_dur)
        current_sum += raw_dur

    # Adjust rounding remainder onto the main conceptual / code card so total sum equals duration_mins EXACTLY
    remainder = round(duration_mins - current_sum, 1)
    if remainder != 0:
        # adjust middle card
        mid_idx = min(len(allocated) - 1, max(0, len(allocated) // 2))
        allocated[mid_idx] = round(allocated[mid_idx] + remainder, 1)

    cards = []
    for i, (item, dur) in enumerate(zip(selected, allocated), 1):
        cards.append({
            "card_index": i,
            "title": item["title"],
            "duration_mins": dur,
            "pedagogical_phase": item["phase"],
            "key_concept": item["concept"],
            "visual_type": item["visual"]
        })

    return cards

def get_mock_cards(class_title: str) -> List[Dict[str, Any]]:
    """Returns sample golden cards for testing UI and teleprompter."""
    return [
        {
            "card_index": 1,
            "title": "Instructor Note & Staging Mechanics",
            "duration_mins": 5.0,
            "target_words": 380,
            "pedagogical_phase": "Instructor Note",
            "stage_directions": "[DO NOT SCREEN SHARE YET. Welcome learners and set pacing expectations.]",
            "content_script": (
                "Welcome everyone to today's masterclass. Before we open any IDE or terminal, "
                "let me set the context: today's topic is often taught as dry theoretical jargon, but by the end "
                "of our 90 minutes together, you will be able to look at any large-scale production system and immediately "
                "point out its bottleneck. Keep your chat window open—we have two live checkpoints today where I will ask you "
                "to vote on real production failure scenarios."
            ),
            "code_snippet": None,
            "code_language": None,
            "quiz_json": None,
            "visual_asset_json": None,
            "is_approved": True
        },
        {
            "card_index": 2,
            "title": "Session Roadmap & Real-World Framing",
            "duration_mins": 4.0,
            "target_words": 320,
            "pedagogical_phase": "Agenda",
            "stage_directions": "[PROJECT ROADMAP ON SCREEN. Point to the three distinct milestones.]",
            "content_script": (
                "Here is our flight plan for tonight. We are attacking this problem in three clear phases: "
                "First, we will examine why the naive, single-server approach crashes under high traffic. "
                "Second, we will build a decoupled worker architecture that survives spikes. "
                "Third, we will deliberately kill one of our worker processes and watch the system self-heal in real time."
            ),
            "code_snippet": None,
            "code_language": None,
            "quiz_json": None,
            "visual_asset_json": json.dumps({
                "type": "mermaid",
                "content": "graph LR\n  A[Phase 1: Naive Crash] --> B[Phase 2: Decoupled Queue]\n  B --> C[Phase 3: Resilient Failover]",
                "caption": "Figure 1.1: Today's 3-Phase Progression"
            }),
            "is_approved": True
        },
        {
            "card_index": 3,
            "title": "The Everyday Analogy: The Swiggy Order Crisis",
            "duration_mins": 6.0,
            "target_words": 440,
            "pedagogical_phase": "Everyday Analogy",
            "stage_directions": "[USE WHITEBOARD. Draw the restaurant kitchen, order counter, and delivery partner.]",
            "content_script": (
                "Imagine you run a popular restaurant on New Year's Eve. Orders are pouring in every second. "
                "What happens if the head chef insists on taking every phone call, noting down the payment, and then cooking the food? "
                "The entire kitchen grinds to a halt! A single slow customer on the phone blocks all 50 other orders. "
                "In software engineering, this is synchronous blocking. What does a real restaurant do? "
                "They place an order slip onto a revolving order wheel, and line cooks pick up slips as soon as they are free. "
                "That order wheel is an event queue. Let us see how this translates into code."
            ),
            "code_snippet": None,
            "code_language": None,
            "quiz_json": None,
            "visual_asset_json": json.dumps({
                "type": "mermaid",
                "content": "graph TD\n  Customer[Incoming Orders] --> Wheel[Revolving Order Slip Wheel]\n  Wheel --> Cook1[Line Cook A]\n  Wheel --> Cook2[Line Cook B]\n  Wheel --> Cook3[Line Cook C]",
                "caption": "Figure 1.2: Physical Analogy of Asynchronous Order Decoupling"
            }),
            "is_approved": True
        },
        {
            "card_index": 4,
            "title": "Checkpoint Quiz 1: Diagnosing the Bottleneck",
            "duration_mins": 4.0,
            "target_words": 280,
            "pedagogical_phase": "Quiz",
            "stage_directions": "[LAUNCH POLL IN CHAT. Give learners 45 seconds before revealing the distractor explanations.]",
            "content_script": (
                "Let's test our intuition before we look at the code. Look at the poll on your screen. "
                "When a sudden traffic spike causes server response times to skyrocket from 20ms to 8000ms, "
                "which component is experiencing the primary bottleneck?"
            ),
            "code_snippet": None,
            "code_language": None,
            "quiz_json": json.dumps({
                "question": "What is the primary cause of sudden latency spikes during unbuffered synchronous request spikes?",
                "options": [
                    "A) Memory heap fragmentation in the client app",
                    "B) Thread pool exhaustion due to blocking I/O calls",
                    "C) Network packet loss at the operating system NIC",
                    "D) Disk filesystem inode exhaustion"
                ],
                "correct_option_index": 1,
                "explanation": "Correct answer is B. When requests block on slow downstream operations, server worker threads are tied up, exhausting the connection thread pool.",
                "distractor_explanations": {
                    "A": "Incorrect. Client heap memory does not dictate server-side latency spikes.",
                    "C": "Incorrect. Packet loss drops connections rather than inflating thread wait queues.",
                    "D": "Incorrect. Inode exhaustion affects file creation on disk, not ephemeral network connections."
                }
            }),
            "visual_asset_json": None,
            "is_approved": True
        },
        {
            "card_index": 5,
            "title": "Code Walkthrough: Minimal Non-Blocking Producer",
            "duration_mins": 7.0,
            "target_words": 420,
            "pedagogical_phase": "Code Along",
            "stage_directions": "[SWITCH TO TERMINAL. Run the test script and show execution duration < 5ms.]",
            "content_script": (
                "Notice the snippet on your screen. In line 3, we do not call the database synchronously. "
                "Instead, we publish an event payload onto our channel and immediately return a 202 Accepted status. "
                "Look at the execution time in the terminal: 3.2 milliseconds. The client gets instant confirmation, "
                "and our background workers handle the heavy lifting asynchronously."
            ),
            "code_snippet": (
                "# Python 3.11 / FastAPI Non-Blocking Dispatch\n"
                "from fastapi import FastAPI, BackgroundTasks\n"
                "\n"
                "app = FastAPI()\n"
                "\n"
                "@app.post('/api/orders')\n"
                "async def create_order(order_data: dict, bg_tasks: BackgroundTasks):\n"
                "    # Non-blocking publish to event queue\n"
                "    bg_tasks.add_task(process_order_async, order_data)\n"
                "    return {'status': 'ACCEPTED', 'order_id': order_data.get('id')}\n"
            ),
            "code_language": "python",
            "quiz_json": None,
            "visual_asset_json": None,
            "is_approved": True
        },
        {
            "card_index": 6,
            "title": "The Master Mental Model Recap",
            "duration_mins": 5.0,
            "target_words": 350,
            "pedagogical_phase": "Recap",
            "stage_directions": "[DISPLAY FINAL MASTER RECAP DIAGRAM. Tell students to take a screenshot for their interview notes.]",
            "content_script": (
                "Take a screenshot of this final diagram. Everything we explored tonight fits into this single architecture: "
                "The API Gateway receives requests, immediately parks them in the partitioned event log, and multiple "
                "worker pools process them in parallel. If a worker dies, consumer group rebalancing kicks in automatically. "
                "In our next class, we will take this exact architecture and scale it across multiple AWS availability zones."
            ),
            "code_snippet": None,
            "code_language": None,
            "quiz_json": None,
            "visual_asset_json": json.dumps({
                "type": "mermaid",
                "content": (
                    "flowchart TD\n"
                    "  Client[Mobile Client] -->|POST /order| GW[API Gateway]\n"
                    "  GW -->|Push Event| Stream[Partitioned Event Stream]\n"
                    "  Stream --> W1[Worker Node 1]\n"
                    "  Stream --> W2[Worker Node 2]\n"
                    "  W1 --> DB[(Primary Store)]\n"
                    "  W2 --> Notify[Push Notifications]"
                ),
                "caption": "Figure 3.1: Complete 90-Minute Production Mental Model"
            }),
            "is_approved": True
        }
    ]

def get_mock_review_results() -> List[Dict[str, Any]]:
    """Returns sample multi-agent review audit results."""
    return [
        {
            "lens_name": "Technical Accuracy",
            "reviewer_persona": "Advanced Lens",
            "score": 9.5,
            "passed": True,
            "summary": "Technical semantics and API dialect compliant with Python 3.11 / FastAPI standards.",
            "detailed_findings": [
                "FastAPI BackgroundTasks usage is syntactically sound.",
                "Non-blocking HTTP 202 Accepted return matches RFC specifications.",
                "Platform and version explicitly stated on code card."
            ]
        },
        {
            "lens_name": "Learner Clarity",
            "reviewer_persona": "Beginner Lens",
            "score": 9.2,
            "passed": True,
            "summary": "Cognitive pacing is smooth; Swiggy kitchen analogy effectively bridges the concept.",
            "detailed_findings": [
                "Zero unexplained jargon in introductory cards.",
                "Plain English explanation accompanies code walkthrough.",
                "Failure mode contrast clearly demonstrates the benefit of decoupling."
            ]
        },
        {
            "lens_name": "Syllabus Coverage & SOP Rules",
            "reviewer_persona": "SOP Linter",
            "score": 9.8,
            "passed": True,
            "summary": "Mathematical time constraint met (Cards sum to exactly 90 mins). Zero em-dashes detected.",
            "detailed_findings": [
                "Durations sum to exactly 90.0 minutes.",
                "All 15 cards within 4-10 minute duration ceiling.",
                "Recap card includes mandatory unified visual mental model.",
                "Quizzes include explicit distractor diagnostic explanations."
            ]
        }
    ]
