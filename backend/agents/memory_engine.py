import json
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from models import MemoryLedger, Class, Script, Module

def _infer_domain_anchor_from_title(title: str, module_name: Optional[str] = None) -> str:
    """
    If no running example exists from a previous class/module or SME,
    infers an authentic production running system tailored to the topic
    instead of a generic default.
    """
    text = f"{title} {module_name or ''}".lower()
    if any(k in text for k in ["sql", "dbms", "database", "query", "schema", "index"]):
        return "Blinkit Inventory & Order Management Database Schema"
    if any(k in text for k in ["cache", "redis", "distributed", "concurrency", "scaling"]):
        return "BookMyShow High-Concurrency Seat Lock & Reservation Cluster"
    if any(k in text for k in ["queue", "kafka", "message", "async", "event"]):
        return "Zerodha Real-Time Stock Order Matching & Trade Settlement Pipeline"
    if any(k in text for k in ["auth", "security", "jwt", "oauth", "payment"]):
        return "Razorpay Instant Merchant Payment Gateway & Verification System"
    if any(k in text for k in ["tree", "graph", "dp", "recursion", "dsa", "algorithm"]):
        return "Google Maps Shortest Path Routing & Delivery Dispatch Graph"
    if any(k in text for k in ["ml", "ai", "model", "neural", "vision", "nlp"]):
        return "Netflix Personalized Content Recommendation & Video Ranking Engine"
    return "Production Distributed E-Commerce Checkout & Inventory Engine"

def retrieve_context_memory(db: Session, class_id: int, manual_context: Optional[str] = None) -> Dict[str, Any]:
    """
    Pulls contextual memory for a class ensuring deep continuity:
    1. If Class N > 1 in the current module:
       - Retrieves terms mastered, code abstractions, and the EXACT running example/analogy
         from Class N-1 to evolve continuously.
       - Inherits the cliffhanger from Class N-1 to open the lecture.
    2. If Class 1 of Module M > 1:
       - Bridges from Module M-1 (retrieves the last class of the previous module,
         its concepts mastered, and the system built there).
       - Establishes why the system built in Module M-1 breaks or requires this new module.
    3. If Class 1 of Module 1:
       - Uses SME-provided example if available, or derives an authentic production system
         matching the domain (never a hardcoded cliché).
    """
    current_class = db.query(Class).filter(Class.id == class_id).first()
    if not current_class:
        return {
            "is_first_class": True,
            "anchor_analogy": _infer_domain_anchor_from_title("Architecture"),
            "terms_mastered": [],
            "code_abstractions": [],
            "cliffhanger": None,
            "previous_module_bridge": None,
            "manual_notes": manual_context
        }

    current_module = current_class.module

    # 1. Look for preceding class in the same module
    prev_class = db.query(Class).filter(
        Class.module_id == current_class.module_id,
        Class.sequence_number < current_class.sequence_number
    ).order_by(Class.sequence_number.desc()).first()

    # 2. Look for succeeding class in the same module
    next_class = db.query(Class).filter(
        Class.module_id == current_class.module_id,
        Class.sequence_number > current_class.sequence_number
    ).order_by(Class.sequence_number.asc()).first()

    # 3. Check for approved historical script for this specific class
    reference_script = db.query(Script).filter(
        Script.class_id == current_class.id,
        Script.status.in_(["GTG", "Approved", "SME Review"])
    ).order_by(Script.id.desc()).first()

    terms: List[str] = []
    abstractions: List[str] = []
    anchor_analogy: Optional[str] = None
    cliffhanger: Optional[str] = None
    previous_module_bridge: Optional[Dict[str, Any]] = None

    if prev_class:
        # CONTINUITY WITHIN THE SAME MODULE (Class N > 1)
        ledger = db.query(MemoryLedger).filter(MemoryLedger.class_id == prev_class.id).first()
        if ledger:
            try:
                terms = json.loads(ledger.terms_mastered) if ledger.terms_mastered else []
            except Exception:
                terms = []
            try:
                abstractions = json.loads(ledger.code_abstractions) if ledger.code_abstractions else []
            except Exception:
                abstractions = []
            anchor_analogy = ledger.anchor_analogy
            cliffhanger = ledger.cliffhanger
        else:
            # Fallback: retrieve approved script from Class N-1 if ledger wasn't explicitly saved
            prev_script = db.query(Script).filter(
                Script.class_id == prev_class.id,
                Script.status.in_(["GTG", "Approved", "SME Review", "Completed"])
            ).order_by(Script.id.desc()).first()
            if prev_script and prev_script.cards:
                terms = [f"Foundations taught in {prev_class.title}"]
            else:
                terms = [f"Core concepts from {prev_class.title}"]
            cliffhanger = f"In the previous class, we built {prev_class.title}. Today, we take that implementation and evolve it."

    else:
        # CLASS 1 OF A MODULE: Check for previous module in the same track to create a bridge
        if current_module:
            prev_module = db.query(Module).filter(
                Module.track_id == current_module.track_id,
                Module.sequence_order < current_module.sequence_order
            ).order_by(Module.sequence_order.desc()).first()

            if prev_module:
                # Find the final concluding class of that previous module
                last_class_of_prev_mod = db.query(Class).filter(
                    Class.module_id == prev_module.id
                ).order_by(Class.sequence_number.desc()).first()

                if last_class_of_prev_mod:
                    prev_mod_ledger = db.query(MemoryLedger).filter(
                        MemoryLedger.class_id == last_class_of_prev_mod.id
                    ).first()

                    prev_mod_terms = []
                    prev_mod_analogy = None
                    if prev_mod_ledger:
                        try:
                            prev_mod_terms = json.loads(prev_mod_ledger.terms_mastered) if prev_mod_ledger.terms_mastered else []
                        except Exception:
                            prev_mod_terms = []
                        prev_mod_analogy = prev_mod_ledger.anchor_analogy

                    previous_module_bridge = {
                        "previous_module_name": prev_module.name,
                        "last_class_title": last_class_of_prev_mod.title,
                        "key_terms_mastered": prev_mod_terms or [f"Foundations of {prev_module.name}"],
                        "running_system_used": prev_mod_analogy,
                        "bridge_directive": (
                            f"Connect from '{prev_module.name}' (specifically '{last_class_of_prev_mod.title}'). "
                            f"Show the transition from the system built in the previous module to motivate why "
                            f"learners now need today's module: '{current_module.name}'."
                        )
                    }
                    # Inherit the running system/analogy from the previous module if available
                    if prev_mod_analogy:
                        anchor_analogy = prev_mod_analogy

    # Dynamic fallback for anchor analogy if neither previous class nor module had one
    if not anchor_analogy:
        anchor_analogy = _infer_domain_anchor_from_title(
            current_class.title,
            current_module.name if current_module else None
        )

    next_class_info = None
    if next_class:
        try:
            next_agendas = json.loads(next_class.agendas) if next_class.agendas else []
        except Exception:
            next_agendas = [next_class.agendas]
        next_class_info = {
            "title": next_class.title,
            "sequence_number": next_class.sequence_number,
            "agendas": next_agendas
        }

    return {
        "is_first_class": prev_class is None,
        "current_class_sequence": current_class.sequence_number,
        "previous_class_title": prev_class.title if prev_class else None,
        "next_class_info": next_class_info,
        "anchor_analogy": anchor_analogy,
        "terms_mastered": terms,
        "code_abstractions": abstractions,
        "cliffhanger": cliffhanger,
        "previous_module_bridge": previous_module_bridge,
        "reference_script_available": reference_script is not None,
        "reference_script_id": reference_script.id if reference_script else None,
        "manual_notes": manual_context
    }

def save_memory_ledger(
    db: Session,
    class_id: int,
    module_id: int,
    terms: List[str],
    abstractions: List[str],
    analogy: Optional[str],
    cliffhanger: Optional[str],
    summary: Optional[Dict[str, Any]] = None
):
    """Saves the memory record of an approved script: its terms, what it built, its running example, how it ended, and (summary) what it taught in order."""
    existing = db.query(MemoryLedger).filter(MemoryLedger.class_id == class_id).first()
    if existing:
        existing.terms_mastered = json.dumps(terms)
        existing.code_abstractions = json.dumps(abstractions)
        existing.anchor_analogy = analogy
        existing.cliffhanger = cliffhanger
        existing.summary_json = json.dumps(summary) if summary else None
    else:
        ledger = MemoryLedger(
            module_id=module_id,
            class_id=class_id,
            terms_mastered=json.dumps(terms),
            code_abstractions=json.dumps(abstractions),
            anchor_analogy=analogy,
            cliffhanger=cliffhanger,
            summary_json=json.dumps(summary) if summary else None
        )
        db.add(ledger)
    db.commit()
