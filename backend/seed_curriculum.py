import os
import re
import json
from database import sync_engine, Base, SyncSessionLocal
from models import Program, Track, Module, Class

CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Scaler_Official_Curriculum_And_Course_Catalog.txt")

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text.strip('-')[:100]

def seed_database():
    print(f"Creating all database tables...")
    Base.metadata.create_all(bind=sync_engine)

    session = SyncSessionLocal()
    try:
        existing_programs = session.query(Program).count()
        if existing_programs >= 5:
            print(f"Database already seeded with {existing_programs} programs.")
            class_count = session.query(Class).count()
            print(f"Total Classes in DB: {class_count}")
            return

        if not os.path.exists(CATALOG_PATH):
            print(f"ERROR: Catalog file not found at {CATALOG_PATH}")
            return

        print(f"Reading curriculum catalog from {CATALOG_PATH}...")
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        current_program = None
        current_track_name = None
        current_module = None
        in_classes_block = False
        class_sequence = 1

        programs_map = {
            "Program 1: Scaler Academy (Software Development)": {
                "name": "Scaler Academy",
                "slug": "scaler-academy",
                "desc": "Software Development (DSA, System Design, Backend, Full Stack)"
            },
            "Program 2: Scaler DSML (Data Science & Machine Learning)": {
                "name": "Scaler DSML",
                "slug": "scaler-dsml",
                "desc": "Data Science & Machine Learning (Business Analytics, Deep Learning, MLOps)"
            },
            "Program 3: Scaler DevOps & Cloud Engineering": {
                "name": "Scaler DevOps & Cloud",
                "slug": "scaler-devops-cloud",
                "desc": "DevOps, SRE, Kubernetes, CI/CD, Infrastructure as Code"
            },
            "Program 4: Scaler AI Forward Deployed Engineer (FDE)": {
                "name": "Scaler AI FDE",
                "slug": "scaler-ai-fde",
                "desc": "Forward Deployed AI Engineering, LLMOps, Enterprise Agentic AI"
            },
            "Program 5: Scaler AI / ML & Agentic AI": {
                "name": "Scaler AI/ML & Agentic AI",
                "slug": "scaler-aiml-agentic-ai",
                "desc": "Advanced AI/ML, Autonomous Agents, Multi-Agent Systems"
            }
        }

        db_programs = {}
        for p_key, p_val in programs_map.items():
            prog = session.query(Program).filter_by(slug=p_val["slug"]).first()
            if not prog:
                prog = Program(name=p_val["name"], slug=p_val["slug"], description=p_val["desc"])
                session.add(prog)
                session.commit()
                session.refresh(prog)
            db_programs[p_key] = prog

        tracks_cache = {}
        def get_or_create_track(prog_id, track_name):
            key = (prog_id, track_name)
            if key in tracks_cache:
                return tracks_cache[key]
            track = session.query(Track).filter_by(program_id=prog_id, name=track_name).first()
            if not track:
                track = Track(program_id=prog_id, name=track_name, slug=slugify(track_name))
                session.add(track)
                session.commit()
                session.refresh(track)
            tracks_cache[key] = track
            return track

        for line_raw in lines:
            line = line_raw.strip()

            for p_key, p_obj in db_programs.items():
                if p_key in line:
                    current_program = p_obj
                    current_track_name = "Core Track"
                    current_module = None
                    in_classes_block = False
                    class_sequence = 1
                    break

            if not current_program:
                continue

            track_match = re.match(r'^\[(.*?)\]$', line)
            if track_match:
                current_track_name = track_match.group(1).strip()
                in_classes_block = False
                continue

            if line.startswith("Module:"):
                mod_name = line.replace("Module:", "").strip()
                track_obj = get_or_create_track(current_program.id, current_track_name or "General Track")
                
                current_module = session.query(Module).filter_by(track_id=track_obj.id, name=mod_name).first()
                if not current_module:
                    current_module = Module(
                        track_id=track_obj.id,
                        name=mod_name,
                        slug=slugify(mod_name),
                        is_locked=True,
                        module_type="Core"
                    )
                    session.add(current_module)
                    session.commit()
                    session.refresh(current_module)
                
                in_classes_block = False
                class_sequence = 1
                continue

            if current_module and line.startswith("Type:"):
                if "Specialisation" in line:
                    current_module.module_type = "Specialisation"
                elif "Elective" in line:
                    current_module.module_type = "Elective"
                else:
                    current_module.module_type = "Core"
                session.commit()
                continue

            if "Classes Covered:" in line or "Topics Covered:" in line:
                in_classes_block = True
                continue

            if in_classes_block and current_module and line.startswith("-"):
                class_title = line.lstrip("-").strip()
                if class_title and not class_title.startswith("*"):
                    existing_class = session.query(Class).filter_by(
                        module_id=current_module.id,
                        sequence_number=class_sequence
                    ).first()

                    if not existing_class:
                        default_agendas = json.dumps([
                            f"Conceptual Foundations of {class_title}",
                            f"Real-World Production Architecture & Failure Modes",
                            f"Hands-On Implementation & Live Problem Walkthrough",
                            f"Key Misconceptions, FAQs & 90-Minute Mental Model Recap"
                        ])

                        new_class = Class(
                            module_id=current_module.id,
                            sequence_number=class_sequence,
                            title=class_title,
                            duration_minutes=90,
                            agendas=default_agendas,
                            learning_outcomes=f"Master foundational concepts and industry best practices for {class_title}.",
                            class_type="Live",
                            module_type=current_module.module_type
                        )
                        session.add(new_class)
                        session.commit()
                    class_sequence += 1
                continue

            if not line:
                in_classes_block = False

        total_progs = session.query(Program).count()
        total_tracks = session.query(Track).count()
        total_modules = session.query(Module).count()
        total_classes = session.query(Class).count()

        print(f"\nSeeding Complete!")
        print(f"Total Programs: {total_progs}")
        print(f"Total Tracks: {total_tracks}")
        print(f"Total Modules: {total_modules}")
        print(f"Total Classes: {total_classes}")

    except Exception as e:
        session.rollback()
        print(f"Error during seeding: {e}")
        raise
    finally:
        session.close()

if __name__ == "__main__":
    seed_database()
