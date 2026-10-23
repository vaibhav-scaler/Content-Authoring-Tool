# Scaler AI Lecture Script Generator

A multi-agent authoring, memory ledger, and quality audit platform designed to generate 90-minute masterclass lecture scripts following the 6-Part Script Anatomy (*Why -> What -> How -> Where -> What breaks*).

---

## Architecture Overview

- **Frontend**: Lightweight vanilla ES6 client (`frontend/index.html`, `frontend/app.js`, `frontend/styles.css`) featuring:
  - Cascading curriculum browser (Programs -> Tracks -> Modules -> Classes).
  - Sequential Card-by-Card GTG authoring workflow.
  - Interactive multi-lens audit panels, Mermaid diagrams, animations, and checkpoint quizzes.
  - Dynamic script exports (`.ipynb`, `.doc`, `.md`, `.py`, `.pdf`, `.txt`).
- **Backend**: FastAPI ASGI application (`backend/main.py`) running on Python 3.11+.
- **Multi-Agent Pipeline**:
  - `agents/shared_context.py`: Block A & Block B pedagogical rules injection.
  - `agents/outliner.py`: Proposes structured 90-minute lecture decks with bloom levels and time pacing.
  - `agents/writer.py`: Generates lecture dialogue, everyday analogies, code implementations, and quizzes.
  - `agents/reviewer.py`: 5-Lens quality inspection (Accuracy, Pedagogy, Engagement, Readability, Timing).
  - `agents/moderator.py`: Surgical repair of flagged cards and timing rebalance.
  - `agents/attributes_generator.py`: Generates Mermaid flowcharts, SVG architectural infographics, and interactive HTML simulations.
- **Database**: SQLite with SQLAlchemy ORM (`models.py`, `database.py`).

---

## Local Development Setup

### 1. Prerequisites
- Python 3.10+ (Recommended Python 3.11)
- Git

### 2. Clone Repository & Setup Virtual Environment
```bash
git clone <your-repo-url>
cd Script_Generator

# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On Linux / macOS:
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r backend/requirements.txt
```

### 4. Configure Environment Variables
Copy `.env.example` to `backend/.env` and insert your API keys:
```bash
cp .env.example backend/.env
```
Key variables:
- `ANTHROPIC_API_KEY`: Claude Sonnet 5.5 (`ANTHROPIC_MODEL`, default `claude-sonnet-5-5`). Writes the outline, every card of the script, the reviews, the repairs, the chat assistant and the HTML animations.
- `GEMINI_API_KEY`: Gemini Flash. Makes the Mermaid flow diagrams (`GEMINI_TEXT_MODEL`) and the pictures (`GEMINI_IMAGE_MODEL`).
- `EXECUTION_MODE`: `live` (calls real LLM APIs) or `mock` (deterministic test outputs).

An Administrator can check that both keys work with `GET /api/system/ai-status` and `POST /api/system/ai-test?image=true`.

How a script is generated: the outline is made first, with a visual card (its own prompt) placed after the card it supports. The
script is then written card by card. Claude writes each card's text and any animation; Gemini makes the Mermaid flows and
images; the full script is complete when the last card is written, and then goes to review.

### 5. Seed the Curriculum Database (If needed)
```bash
cd backend
python -m seed_curriculum
```

### 6. Run the Application
```bash
cd backend
python -m uvicorn main:app --reload --port 8000
```
Open your browser at `http://localhost:8000` to access the full UI and backend APIs.
