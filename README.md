# 📅 Teacher Timetable App

A modular Streamlit application for viewing and filtering school timetables by teacher and class.

## Project Structure

```
Timetable/
├── app.py                           # Main Streamlit application
├── requirements.txt                 # Python dependencies
├── data/
│   ├── teachers.csv                 # Teacher information
│   └── generated_timetable.csv       # Generated timetable schedule
├── repository/
│   ├── __init__.py
│   └── data_repository.py            # DataRepository class for CSV handling
└── scheduler/                        # (Reserved for future scheduling logic)
```

## Key Components

### 1. `DataRepository` (repository/data_repository.py)
Handles all CSV file operations in a clean, reusable way:
- Load timetable from CSV
- Get unique teachers and classes
- Filter by teacher name
- Filter by class name
- Filter by both teacher and class

**Why modular?** Makes it easy to swap CSV for database/API later without changing the app.

### 2. `app.py` - Streamlit Interface
Features:
- **Sidebar filters**: Select teacher and class
- **Two views**:
  - List view: Traditional table format
  - Grid view: Pivot table by Day/Period
- **Real-time filtering**: Filters apply instantly
- **Summary statistics**: Shows number of teachers, classes, and time slots

## Setup & Installation

### 1. Create a Virtual Environment (Optional but Recommended)
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the App
```bash
streamlit run app.py
```

The app will open at `http://localhost:8501`

## Usage

1. **View Full Timetable**: Select "All Teachers" and "All Classes"
2. **Filter by Teacher**: Select a teacher name to see only their classes
3. **Filter by Class**: Select a class name to see all teachers for that class
4. **Combined Filter**: Select both to see a specific teacher-class combination
5. **Switch Views**: Use tabs to view as list or grid (pivot table)

## CSV Format

The `generated_timetable.csv` must have columns:
```
Class, Day, Period, Teacher
Class 10A, Monday, 1, Ms. Nguyen
...
```

## Code Philosophy

- **Beginner-friendly**: Clear variable names, detailed comments
- **Modular**: Separation of concerns (data handling vs UI)
- **Simple**: No complex frameworks, just Pandas + Streamlit
- **Extensible**: Easy to add features without rewriting

## Next Steps

Future enhancements could include:
- Load from database instead of CSV
- Add teacher/class information details
- Export filtered timetable to PDF
- Conflict detection and highlighting
- AI-powered scheduling optimization
- User authentication and role-based access

---

**Created**: 2026-05-18 | **Tech**: Python, Streamlit, Pandas
