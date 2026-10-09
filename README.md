# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing employee attendance tracking (punch-in, punch-out, regularization audit trail) and analytics aggregation pipelines (monthly summaries, department summaries, late leaderboard, department trend).

## Requirements

- Python 3.11+
- MongoDB 6.0+ (Local Docker container or MongoDB Atlas cluster)

## Setup & Running

1. **Clone the repository and set up a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables:**
   Create a `.env` file or export environment variables:
   ```bash
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```

3. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

   The service starts at `http://127.0.0.1:8000`. Database indexes are automatically created at application startup.

## Testing & Verification

Interactive API documentation (Swagger UI) is available at:
`http://127.0.0.1:8000/docs`

Readiness health probe:
`GET /health`

## Documentation Files

- `REVIEW.md`: Detailed code review of initial starter defects and explanations of fixes.
- `DECISIONS.md`: Design rationale for database indexes, concurrency handling, tie resolution, headcount calculation, and scale-out architecture.
