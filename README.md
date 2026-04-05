# HireAI – Autonomous Recruiting System

HireAI is a system that automates the hiring process. It can:
- collect candidate data  
- score and rank candidates  
- send recruiter-style emails  
- detect copied or AI-generated answers  
- improve itself over time  

In short: it works like an automated recruiter.

---

## Honest Note

This project focuses on real execution, not perfection.

Instead of showing a polished system, it shows:
- real attempts (including failures)
- actual logs, tests, and fixes
- clear notes on what worked and what didn’t

---

## What This Project Actually Implements

- CSV/XLSX data ingestion  
- candidate scoring and tiering  
- Gmail-based engagement (with SMTP fallback)  
- AI, timing, and similarity-based anti-cheat checks  
- code submission review flow  
- self-learning with database-backed scoring updates  
- queue-based background processing (Celery + Redis)  
- scheduled polling and learning (Celery Beat)  
- frontend visibility for:
  - system status  
  - scoring weights  
  - review notes  

---

## What Is NOT Fully Proven Yet

- no successful Internshala login automation  
- no complete live scraping run recorded  
- no real test account evidence included  
- no reCAPTCHA failure screenshots  
- no browser logs from live automation attempts  

These parts are still experimental and should not be overstated.

---

## Workflow

---

## Full Step-by-Step Flow

1. Candidate data enters the system via:
   - CSV/XLSX upload  
   - or platform access  

2. Data is cleaned and converted into a standard format.

3. The scoring system evaluates each candidate based on:
   - skills  
   - answer quality  
   - GitHub profile  
   - completeness  
   - penalties  

4. A final score and tier are assigned:
   - Fast-track  
   - Standard  
   - Review  
   - Reject  

5. Eligible candidates receive an automated recruiter-style email.

6. The system reads replies from Gmail threads.

7. Anti-cheat checks run:
   - AI-generated content detection  
   - suspiciously fast replies  
   - similarity across candidates  

8. Based on responses:
   - follow-up questions are sent  
   - or candidates are moved forward/rejected  

9. All interactions are logged and stored.

10. After every batch:
    - patterns are analyzed  
    - scoring weights are updated  

11. The system improves future decisions using these updates.

---

## Tech Stack

- Backend: Flask  
- Database: SQLAlchemy + SQLite  
- Queue: Celery  
- Broker: Redis  
- Scheduler: Celery Beat  
- LLM: Groq  
- Email: Gmail API  
- Backup email: SMTP  
- File parsing: pandas, openpyxl  
- Similarity detection: scikit-learn  

---

## Setup Instructions

### 1. Clone the repository
``bash
        
    git clone <github.com/rarihant14/HireAI---Autonomous-Recruiting-System>
    cd hiring-agent



2. Create virtual environment

       python -m venv venv

3. Activate environment

Windows:

      venv\Scripts\activate

Linux/macOS:

    source venv/bin/activate


4. Install dependencies

       pip install -r requirements.txt
6. Configure environment variables

Create a .env file:

    GROQ_API_KEY=your_groq_key

For Gmail:

place credentials.json in project root


Contribution Note
UI layer: AI-assisted
Backend logic, system design, and integrations: ~70% manually built
