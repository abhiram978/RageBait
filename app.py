#!/usr/bin/env python3
"""
The Greed Trial — Backend Server v2
Handles: trivia questions, user accounts, score tracking, graph predictions,
         individual stats, leaderboard auto-refresh, anti-spam protection
"""

import http.server
import json
import os
import time
import random
import hashlib
import urllib.request
import urllib.parse
import html
import threading
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.error import URLError, HTTPError


def generate_pattern(level):
    patterns = {
        "easy": [1, 1, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1, 0, 1],
        "medium": [1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1],
        "hard": [1, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0],
        "brutal": [1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0],
    }
    if level <= 4:
        pat = list(patterns["easy"])
    elif level <= 10:
        pat = list(patterns["medium"])
    elif level <= 16:
        pat = list(patterns["hard"])
    else:
        pat = list(patterns["brutal"])
        
    for i in range(len(pat)-1, 0, -1):
        if random.random() < 0.2:
            j = max(0, i - random.randint(1, 3))
            pat[i], pat[j] = pat[j], pat[i]
    return pat

def get_next_outcome(token):
    with game_lock:
        state = active_games.get(token)
        if not state:
            return 0
        if "pattern" not in state or state["pattern_pos"] >= len(state["pattern"]):
            state["pattern"] = generate_pattern(state["level"])
            state["pattern_pos"] = 0
            
        result = state["pattern"][state["pattern_pos"]]
        state["pattern_pos"] += 1
        
        if result == 1 and state["money"] > 500000 and random.random() < 0.15:
            return 0
        if result == 0 and state["streak"] >= 5 and random.random() < 0.1:
            return 1
            
        return result

# ============================================================
# CONFIG
# ============================================================
PORT = 8080
DB_FILE = "greed_trial_db.json"
TRIVIA_CACHE_FILE = "trivia_cache.json"

# Rate limit: OpenTDB allows 1 request per 5 seconds
TRIVIA_FETCH_DELAY = 6  # seconds between API calls
TRIVIA_BATCH_SIZE = 50  # max per request
TRIVIA_MIN_CACHE = 10   # minimum before refetch attempt

# Leaderboard auto-refresh interval
LEADERBOARD_REFRESH_INTERVAL = 60  # seconds (1 minute)

# ============================================================
# DATABASE (simple JSON file)
# ============================================================
db_lock = threading.Lock()
active_games = {}
game_lock = threading.Lock()

# Cached leaderboard for fast access
cached_leaderboard = []
lb_lock = threading.Lock()

def load_db():
    with db_lock:
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r") as f:
                    return json.load(f)
            except:
                pass
    return {"users": {}, "sessions": {}, "leaderboard": []}

def save_db(data):
    with db_lock:
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=2)

def get_user(db, username):
    return db.get("users", {}).get(username)

def create_user(db, username, password, emoji):
    if username in db.get("users", {}):
        return None
    hashed = hashlib.sha256(password.encode()).hexdigest()
    user = {
        "username": username,
        "password_hash": hashed,
        "emoji": emoji,
        "created_at": time.time(),
        "high_score": 0,
        "total_crashes": 0,
        "total_games": 0,
        "total_wins": 0,
        "best_streak": 0,
        "total_clicks": 0,
        "total_cashouts": 0,
        "peak_level": 0,
        "total_play_time": 0,
        "last_played": 0,
        "game_history": [],  # last 20 games
        "achievements": [],
    }
    db.setdefault("users", {})[username] = user
    save_db(db)
    return user

def auth_user(db, username, password):
    user = get_user(db, username)
    if not user:
        return None
    hashed = hashlib.sha256(password.encode()).hexdigest()
    if user["password_hash"] == hashed:
        return user
    return None

def create_session(db, username):
    token = str(uuid.uuid4())
    db.setdefault("sessions", {})[token] = {
        "username": username,
        "created_at": time.time()
    }
    save_db(db)
    return token

def get_session_user(db, token):
    sess = db.get("sessions", {}).get(token)
    if not sess:
        return None
    # Sessions expire after 24 hours
    if time.time() - sess["created_at"] > 86400:
        del db["sessions"][token]
        save_db(db)
        return None
    return sess["username"]

def update_score(db, username, score, crashes, streak, extra_stats=None):
    user = get_user(db, username)
    if not user:
        return
    user["total_games"] = user.get("total_games", 0) + 1
    user["total_crashes"] = user.get("total_crashes", 0) + crashes
    if score > user.get("high_score", 0):
        user["high_score"] = score
    if streak > user.get("best_streak", 0):
        user["best_streak"] = streak
    if score >= 1000000:
        user["total_wins"] = user.get("total_wins", 0) + 1
    
    # Update extra stats if provided
    if extra_stats:
        user["total_clicks"] = user.get("total_clicks", 0) + extra_stats.get("clicks", 0)
        user["total_cashouts"] = user.get("total_cashouts", 0) + extra_stats.get("cashouts", 0)
        user["peak_level"] = max(user.get("peak_level", 0), extra_stats.get("level", 0))
        user["total_play_time"] = user.get("total_play_time", 0) + extra_stats.get("play_time", 0)
        user["last_played"] = time.time()
    
    # Store game history (last 20)
    history = user.get("game_history", [])
    history.append({
        "score": score,
        "crashes": crashes,
        "streak": streak,
        "time": time.time(),
        "level": extra_stats.get("level", 0) if extra_stats else 0,
    })
    user["game_history"] = history[-20:]  # keep last 20
    
    db["users"][username] = user
    
    # Update leaderboard
    lb = db.get("leaderboard", [])
    filtered_lb = [entry for entry in lb if entry["username"] != username]
    filtered_lb.append({
        "username": username,
        "emoji": user.get("emoji", "😀"),
        "score": user.get("high_score", score),
        "crashes": user.get("total_crashes", crashes),
        "total_games": user.get("total_games", 1),
        "total_wins": user.get("total_wins", 0),
        "best_streak": user.get("best_streak", 0),
        "time": time.time()
    })
    filtered_lb.sort(key=lambda x: -x["score"])
    db["leaderboard"] = filtered_lb[:100]
    save_db(db)

def get_user_stats(db, username):
    """Get detailed stats for a specific user"""
    user = get_user(db, username)
    if not user:
        return None
    return {
        "username": username,
        "emoji": user.get("emoji", "😀"),
        "high_score": user.get("high_score", 0),
        "total_games": user.get("total_games", 0),
        "total_crashes": user.get("total_crashes", 0),
        "total_wins": user.get("total_wins", 0),
        "best_streak": user.get("best_streak", 0),
        "total_clicks": user.get("total_clicks", 0),
        "total_cashouts": user.get("total_cashouts", 0),
        "peak_level": user.get("peak_level", 0),
        "total_play_time": user.get("total_play_time", 0),
        "last_played": user.get("last_played", 0),
        "game_history": user.get("game_history", [])[-10:],
    }

def refresh_leaderboard():
    """Refresh the cached leaderboard from DB"""
    global cached_leaderboard
    db = load_db()
    lb = db.get("leaderboard", [])
    lb.sort(key=lambda x: -x.get("score", 0))
    with lb_lock:
        cached_leaderboard = lb[:50]

def leaderboard_refresh_thread():
    """Background thread to refresh leaderboard every minute"""
    while True:
        try:
            refresh_leaderboard()
            print(f"[INFO] Leaderboard refreshed: {len(cached_leaderboard)} entries")
        except Exception as e:
            print(f"[WARN] Leaderboard refresh error: {e}")
        time.sleep(LEADERBOARD_REFRESH_INTERVAL)

# ============================================================
# TRIVIA QUESTION SYSTEM
# ============================================================
# Massive fallback question banks so we never run dry

FALLBACK_EASY = [
    {"question": "What planet is known as the Red Planet?", "correct_answer": "Mars", "incorrect_answers": ["Venus", "Jupiter", "Saturn"]},
    {"question": "How many continents are there on Earth?", "correct_answer": "7", "incorrect_answers": ["5", "6", "8"]},
    {"question": "What is the largest ocean on Earth?", "correct_answer": "Pacific Ocean", "incorrect_answers": ["Atlantic Ocean", "Indian Ocean", "Arctic Ocean"]},
    {"question": "What gas do plants absorb from the atmosphere?", "correct_answer": "Carbon Dioxide", "incorrect_answers": ["Oxygen", "Nitrogen", "Helium"]},
    {"question": "Which animal is known as the King of the Jungle?", "correct_answer": "Lion", "incorrect_answers": ["Tiger", "Elephant", "Bear"]},
    {"question": "What is the chemical symbol for water?", "correct_answer": "H2O", "incorrect_answers": ["CO2", "O2", "NaCl"]},
    {"question": "How many legs does a spider have?", "correct_answer": "8", "incorrect_answers": ["6", "10", "12"]},
    {"question": "What color are emeralds?", "correct_answer": "Green", "incorrect_answers": ["Blue", "Red", "Yellow"]},
    {"question": "Which country is home to the kangaroo?", "correct_answer": "Australia", "incorrect_answers": ["New Zealand", "South Africa", "Brazil"]},
    {"question": "What is the hardest natural substance on Earth?", "correct_answer": "Diamond", "incorrect_answers": ["Gold", "Iron", "Platinum"]},
    {"question": "How many colors are in a rainbow?", "correct_answer": "7", "incorrect_answers": ["5", "6", "8"]},
    {"question": "What is the largest mammal in the world?", "correct_answer": "Blue Whale", "incorrect_answers": ["Elephant", "Giraffe", "Hippopotamus"]},
    {"question": "Which planet is closest to the Sun?", "correct_answer": "Mercury", "incorrect_answers": ["Venus", "Earth", "Mars"]},
    {"question": "What is the boiling point of water in Celsius?", "correct_answer": "100", "incorrect_answers": ["90", "110", "120"]},
    {"question": "How many days are in a leap year?", "correct_answer": "366", "incorrect_answers": ["365", "367", "364"]},
    {"question": "What is the smallest prime number?", "correct_answer": "2", "incorrect_answers": ["1", "3", "0"]},
    {"question": "Which organ pumps blood through the body?", "correct_answer": "Heart", "incorrect_answers": ["Lungs", "Brain", "Liver"]},
    {"question": "What fruit is known for keeping doctors away?", "correct_answer": "Apple", "incorrect_answers": ["Banana", "Orange", "Grape"]},
    {"question": "How many sides does a triangle have?", "correct_answer": "3", "incorrect_answers": ["4", "5", "6"]},
    {"question": "What is the freezing point of water in Celsius?", "correct_answer": "0", "incorrect_answers": ["-10", "10", "32"]},
    {"question": "What is the capital of France?", "correct_answer": "Paris", "incorrect_answers": ["London", "Berlin", "Madrid"]},
    {"question": "Which season comes after winter?", "correct_answer": "Spring", "incorrect_answers": ["Summer", "Autumn", "Winter"]},
    {"question": "How many months have 31 days?", "correct_answer": "7", "incorrect_answers": ["5", "6", "8"]},
    {"question": "What color is a ruby?", "correct_answer": "Red", "incorrect_answers": ["Blue", "Green", "Purple"]},
    {"question": "What do bees produce?", "correct_answer": "Honey", "incorrect_answers": ["Milk", "Silk", "Wax"]},
    {"question": "Which is the longest river in the world?", "correct_answer": "Nile", "incorrect_answers": ["Amazon", "Mississippi", "Yangtze"]},
    {"question": "How many weeks are in a year?", "correct_answer": "52", "incorrect_answers": ["48", "50", "54"]},
    {"question": "What is the opposite of 'hot'?", "correct_answer": "Cold", "incorrect_answers": ["Warm", "Cool", "Freezing"]},
    {"question": "Which shape has 4 equal sides?", "correct_answer": "Square", "incorrect_answers": ["Rectangle", "Triangle", "Circle"]},
    {"question": "What is 12 x 12?", "correct_answer": "144", "incorrect_answers": ["124", "132", "156"]},
    {"question": "What animal says 'moo'?", "correct_answer": "Cow", "incorrect_answers": ["Sheep", "Pig", "Horse"]},
    {"question": "How many hours are in a day?", "correct_answer": "24", "incorrect_answers": ["12", "20", "36"]},
    {"question": "What is the capital of Japan?", "correct_answer": "Tokyo", "incorrect_answers": ["Kyoto", "Osaka", "Seoul"]},
    {"question": "Which element has the chemical symbol 'O'?", "correct_answer": "Oxygen", "incorrect_answers": ["Gold", "Osmium", "Oganesson"]},
    {"question": "What is the largest desert in the world?", "correct_answer": "Sahara", "incorrect_answers": ["Gobi", "Kalahari", "Antarctic"]},
    {"question": "How many zeros are in one million?", "correct_answer": "6", "incorrect_answers": ["5", "7", "8"]},
    {"question": "What primary color is made by mixing red and blue?", "correct_answer": "Purple", "incorrect_answers": ["Green", "Orange", "Brown"]},
    {"question": "Which planet has rings around it?", "correct_answer": "Saturn", "incorrect_answers": ["Mars", "Venus", "Mercury"]},
    {"question": "What is the main ingredient in bread?", "correct_answer": "Flour", "incorrect_answers": ["Sugar", "Salt", "Butter"]},
    {"question": "How many strings does a standard guitar have?", "correct_answer": "6", "incorrect_answers": ["4", "5", "8"]},
]

FALLBACK_MEDIUM = [
    {"question": "What year did the Titanic sink?", "correct_answer": "1912", "incorrect_answers": ["1905", "1915", "1920"]},
    {"question": "Which element has the atomic number 79?", "correct_answer": "Gold", "incorrect_answers": ["Silver", "Platinum", "Copper"]},
    {"question": "What is the speed of light in km/s (approximately)?", "correct_answer": "300,000", "incorrect_answers": ["150,000", "500,000", "1,000,000"]},
    {"question": "Who painted the Mona Lisa?", "correct_answer": "Leonardo da Vinci", "incorrect_answers": ["Michelangelo", "Raphael", "Donatello"]},
    {"question": "What is the powerhouse of the cell?", "correct_answer": "Mitochondria", "incorrect_answers": ["Nucleus", "Ribosome", "Golgi Body"]},
    {"question": "Which country has the most people?", "correct_answer": "India", "incorrect_answers": ["China", "USA", "Indonesia"]},
    {"question": "What is the square root of 169?", "correct_answer": "13", "incorrect_answers": ["11", "12", "14"]},
    {"question": "In what year did World War II end?", "correct_answer": "1945", "incorrect_answers": ["1944", "1946", "1943"]},
    {"question": "What is the currency of Japan?", "correct_answer": "Yen", "incorrect_answers": ["Won", "Yuan", "Rupee"]},
    {"question": "Which blood type is the universal donor?", "correct_answer": "O negative", "incorrect_answers": ["A positive", "AB positive", "B negative"]},
    {"question": "How many bones are in the adult human body?", "correct_answer": "206", "incorrect_answers": ["196", "216", "186"]},
    {"question": "What is the chemical formula for table salt?", "correct_answer": "NaCl", "incorrect_answers": ["KCl", "CaCl2", "NaOH"]},
    {"question": "Which planet is known as the Morning Star?", "correct_answer": "Venus", "incorrect_answers": ["Mars", "Mercury", "Jupiter"]},
    {"question": "What does DNA stand for?", "correct_answer": "Deoxyribonucleic Acid", "incorrect_answers": ["Dinitrogen Acid", "Dynamic Nuclear Acid", "Dual Nucleic Acid"]},
    {"question": "Who wrote 'Romeo and Juliet'?", "correct_answer": "William Shakespeare", "incorrect_answers": ["Charles Dickens", "Jane Austen", "Mark Twain"]},
    {"question": "What is the smallest country in the world?", "correct_answer": "Vatican City", "incorrect_answers": ["Monaco", "San Marino", "Liechtenstein"]},
    {"question": "How many chromosomes do humans have?", "correct_answer": "46", "incorrect_answers": ["44", "48", "42"]},
    {"question": "What is the tallest mountain in the world?", "correct_answer": "Mount Everest", "incorrect_answers": ["K2", "Kangchenjunga", "Makalu"]},
    {"question": "Which gas makes up about 78% of Earth's atmosphere?", "correct_answer": "Nitrogen", "incorrect_answers": ["Oxygen", "Carbon Dioxide", "Argon"]},
    {"question": "What year was the first iPhone released?", "correct_answer": "2007", "incorrect_answers": ["2005", "2008", "2006"]},
]

FALLBACK_HARD = [
    {"question": "What is the half-life of Carbon-14 (in years)?", "correct_answer": "5,730", "incorrect_answers": ["3,200", "8,400", "11,460"]},
    {"question": "In what year was the Treaty of Westphalia signed?", "correct_answer": "1648", "incorrect_answers": ["1588", "1712", "1555"]},
    {"question": "What is the Planck constant (in J·s)?", "correct_answer": "6.626 x 10^-34", "incorrect_answers": ["3.14 x 10^-34", "9.81 x 10^-34", "1.38 x 10^-23"]},
    {"question": "Which mathematician proved Fermat's Last Theorem?", "correct_answer": "Andrew Wiles", "incorrect_answers": ["Pierre de Fermat", "Leonhard Euler", "Carl Gauss"]},
    {"question": "What is the deepest point in the ocean?", "correct_answer": "Mariana Trench", "incorrect_answers": ["Tonga Trench", "Java Trench", "Puerto Rico Trench"]},
    {"question": "What element has the highest melting point?", "correct_answer": "Tungsten", "incorrect_answers": ["Iron", "Titanium", "Carbon"]},
    {"question": "Who developed the theory of General Relativity?", "correct_answer": "Albert Einstein", "incorrect_answers": ["Isaac Newton", "Niels Bohr", "Max Planck"]},
    {"question": "What is the capital of Mongolia?", "correct_answer": "Ulaanbaatar", "incorrect_answers": ["Astana", "Bishkek", "Tashkent"]},
    {"question": "In computing, what does RAID stand for?", "correct_answer": "Redundant Array of Independent Disks", "incorrect_answers": ["Random Access Internal Drive", "Rapid Array of Integrated Data", "Recoverable Archive of Internal Disks"]},
    {"question": "What is the longest bone in the human body?", "correct_answer": "Femur", "incorrect_answers": ["Tibia", "Humerus", "Fibula"]},
    {"question": "Which artist cut off part of his own ear?", "correct_answer": "Vincent van Gogh", "incorrect_answers": ["Pablo Picasso", "Claude Monet", "Salvador Dali"]},
    {"question": "What is the most abundant element in the universe?", "correct_answer": "Hydrogen", "incorrect_answers": ["Helium", "Oxygen", "Carbon"]},
    {"question": "In what year did the Berlin Wall fall?", "correct_answer": "1989", "incorrect_answers": ["1987", "1991", "1985"]},
    {"question": "What is the only mammal capable of true flight?", "correct_answer": "Bat", "incorrect_answers": ["Flying Squirrel", "Sugar Glider", "Colugo"]},
    {"question": "What language has the most native speakers?", "correct_answer": "Mandarin Chinese", "incorrect_answers": ["English", "Spanish", "Hindi"]},
]

# ============================================================
# TRIVIA CACHE with robust fetching
# ============================================================
trivia_cache = {"easy": [], "medium": [], "hard": []}
trivia_lock = threading.Lock()
last_fetch_time = 0

def load_trivia_cache():
    global trivia_cache
    if os.path.exists(TRIVIA_CACHE_FILE):
        try:
            with open(TRIVIA_CACHE_FILE, "r") as f:
                cached = json.load(f)
                if isinstance(cached, dict):
                    trivia_cache = cached
                    print(f"[INFO] Loaded trivia cache: easy={len(trivia_cache.get('easy', []))}, medium={len(trivia_cache.get('medium', []))}, hard={len(trivia_cache.get('hard', []))}")
                    return
        except:
            pass
    # Initialize with fallbacks
    trivia_cache = {
        "easy": list(FALLBACK_EASY),
        "medium": list(FALLBACK_MEDIUM),
        "hard": list(FALLBACK_HARD),
    }
    save_trivia_cache()
    print(f"[INFO] Initialized trivia cache with fallbacks: easy={len(trivia_cache['easy'])}, medium={len(trivia_cache['medium'])}, hard={len(trivia_cache['hard'])}")

def save_trivia_cache():
    try:
        with open(TRIVIA_CACHE_FILE, "w") as f:
            json.dump(trivia_cache, f)
    except Exception as e:
        print(f"[WARN] Could not save trivia cache: {e}")

def fetch_trivia_from_api(difficulty, amount=30):
    """Fetch from OpenTDB with rate limiting and error handling"""
    global last_fetch_time
    
    # Rate limiting
    now = time.time()
    wait = TRIVIA_FETCH_DELAY - (now - last_fetch_time)
    if wait > 0:
        time.sleep(wait)
    
    try:
        url = f"https://opentdb.com/api.php?amount={amount}&difficulty={difficulty}&type=multiple"
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'GreedTrial/1.0')
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            last_fetch_time = time.time()
            data = json.loads(resp.read().decode())
            
            if data.get("response_code") == 0:
                questions = []
                for q in data.get("results", []):
                    questions.append({
                        "question": html.unescape(q["question"]),
                        "correct_answer": html.unescape(q["correct_answer"]),
                        "incorrect_answers": [html.unescape(a) for a in q["incorrect_answers"]],
                        "category": html.unescape(q.get("category", "General")),
                    })
                return questions
            elif data.get("response_code") == 5:
                print(f"[WARN] Rate limited by OpenTDB API")
                last_fetch_time = time.time() + 10  # Extra wait
                return []
            else:
                print(f"[WARN] OpenTDB returned code {data.get('response_code')}")
                return []
    except HTTPError as e:
        print(f"[WARN] Trivia API HTTP error: {e.code}")
        last_fetch_time = time.time() + 10
        return []
    except (URLError, Exception) as e:
        print(f"[WARN] Trivia API error: {e}")
        return []

def prefetch_trivia():
    """Background thread to keep cache full"""
    global trivia_cache
    
    # Start with fallbacks always available
    with trivia_lock:
        for diff, fallback in [("easy", FALLBACK_EASY), ("medium", FALLBACK_MEDIUM), ("hard", FALLBACK_HARD)]:
            if len(trivia_cache.get(diff, [])) < TRIVIA_MIN_CACHE:
                trivia_cache[diff] = list(fallback)
    
    # Then try API
    for difficulty in ["easy", "medium", "hard"]:
        with trivia_lock:
            current_count = len(trivia_cache.get(difficulty, []))
        
        if current_count >= 30:
            print(f"[INFO] {difficulty}: already have {current_count} questions, skipping API")
            continue
            
        print(f"[INFO] Fetching {difficulty} questions from API...")
        questions = fetch_trivia_from_api(difficulty, 30)
        
        if questions:
            with trivia_lock:
                # Merge with existing, deduplicate by question text
                existing_qs = {q["question"] for q in trivia_cache.get(difficulty, [])}
                new_qs = [q for q in questions if q["question"] not in existing_qs]
                trivia_cache[difficulty] = trivia_cache.get(difficulty, []) + new_qs
                print(f"[INFO] {difficulty}: added {len(new_qs)} new, total {len(trivia_cache[difficulty])}")
        else:
            # Ensure fallbacks are loaded
            with trivia_lock:
                fallback = {"easy": FALLBACK_EASY, "medium": FALLBACK_MEDIUM, "hard": FALLBACK_HARD}[difficulty]
                if len(trivia_cache.get(difficulty, [])) < len(fallback):
                    existing_qs = {q["question"] for q in trivia_cache.get(difficulty, [])}
                    new_fb = [q for q in fallback if q["question"] not in existing_qs]
                    trivia_cache[difficulty] = trivia_cache.get(difficulty, []) + new_fb
                print(f"[INFO] {difficulty}: using {len(trivia_cache[difficulty])} questions (fallback)")
        
        time.sleep(TRIVIA_FETCH_DELAY)
    
    save_trivia_cache()
    print(f"[INFO] Trivia cache ready: easy={len(trivia_cache.get('easy', []))}, medium={len(trivia_cache.get('medium', []))}, hard={len(trivia_cache.get('hard', []))}")

def refetch_thread():
    """Periodically refetch trivia in background"""
    while True:
        time.sleep(300)  # Every 5 minutes
        try:
            for difficulty in ["easy", "medium", "hard"]:
                with trivia_lock:
                    count = len(trivia_cache.get(difficulty, []))
                if count < 15:
                    qs = fetch_trivia_from_api(difficulty, 20)
                    if qs:
                        with trivia_lock:
                            existing = {q["question"] for q in trivia_cache.get(difficulty, [])}
                            new_qs = [q for q in qs if q["question"] not in existing]
                            trivia_cache[difficulty] = trivia_cache.get(difficulty, []) + new_qs
                        save_trivia_cache()
                    time.sleep(TRIVIA_FETCH_DELAY)
        except:
            pass

def get_trivia_question(difficulty="easy", count=1):
    """Get question(s) from cache, with guaranteed fallback"""
    with trivia_lock:
        pool = trivia_cache.get(difficulty, [])
        if not pool:
            # Emergency fallback
            fallback = {"easy": FALLBACK_EASY, "medium": FALLBACK_MEDIUM, "hard": FALLBACK_HARD}
            pool = fallback.get(difficulty, FALLBACK_EASY)
            trivia_cache[difficulty] = list(pool)
        
        if count >= len(pool):
            selected = list(pool)
            random.shuffle(selected)
        else:
            selected = random.sample(pool, count)
        
        # Format for client
        result = []
        for q in selected:
            answers = list(q["incorrect_answers"]) + [q["correct_answer"]]
            random.shuffle(answers)
            result.append({
                "question": q["question"],
                "answers": answers,
                "correct": q["correct_answer"],
                "category": q.get("category", "General"),
            })
        return result

# ============================================================
# GRAPH PREDICTION DATA GENERATOR
# ============================================================
def generate_graph_data():
    """Generate a fake stock/crypto chart with a hidden outcome"""
    # Generate 20 historical points
    points = [100]
    for i in range(19):
        change = random.uniform(-8, 8)
        # Add some trend
        if random.random() < 0.6:
            change += random.uniform(-2, 2)
        points.append(max(10, points[-1] + change))
    
    # Decide actual outcome (slightly weighted by recent trend)
    recent_trend = points[-1] - points[-5] if len(points) >= 5 else 0
    if recent_trend > 5:
        # Was going up - 45% chance continues (trap!)
        goes_up = random.random() < 0.45
    elif recent_trend < -5:
        # Was going down - 45% chance continues
        goes_up = random.random() > 0.45
    else:
        goes_up = random.random() < 0.5
    
    # Generate the "reveal" points
    reveal = [points[-1]]
    for i in range(5):
        if goes_up:
            reveal.append(reveal[-1] + random.uniform(1, 8))
        else:
            reveal.append(max(5, reveal[-1] - random.uniform(1, 8)))
    
    return {
        "history": [round(p, 2) for p in points],
        "reveal": [round(p, 2) for p in reveal],
        "goes_up": goes_up,
        "asset_name": random.choice([
            "GREED/USD", "COPE/BTC", "FOMO.X", "REKT-ETF",
            "PUMP&DUMP", "BAGS.IO", "MOON/SOL", "RUG.PULL"
        ])
    }

# ============================================================
# BITCOIN INVESTMENT GENERATOR
# ============================================================
def generate_bitcoin_opportunity():
    """Generate a Bitcoin/crypto investment opportunity that backfires 67% of the time"""
    opportunities = [
        {
            "name": "🪙 Bitcoin Flash Dip",
            "desc": "BTC dropped 12% in 2 hours. Buy the dip?",
            "invest_msg": "You bought the dip!",
            "skip_msg": "You played it safe.",
            "backfire_chance": 0.67,
            "win_mult": 2.5,
            "lose_mult": 0.3,
            "win_msg": "📈 BTC recovered! Your investment 2.5x'd!",
            "lose_msg": "📉 BTC kept dropping. Lost 70% of your investment. The dip keeps dipping.",
        },
        {
            "name": "🐕 MemeCoin Launch",
            "desc": "New memecoin '$GREED' just launched. 10,000% potential?",
            "invest_msg": "You aped in!",
            "skip_msg": "FOMO avoided.",
            "backfire_chance": 0.67,
            "win_mult": 5.0,
            "lose_mult": 0.1,
            "win_msg": "🚀 $GREED mooned! 5x return! (this never happens)",
            "lose_msg": "🔻 Rug pulled. Dev sold everything. Lost 90%. Classic.",
        },
        {
            "name": "⛏️ Mining Contract",
            "desc": "Cloud mining contract: 'Guaranteed 200% APY'. Legit?",
            "invest_msg": "You signed the contract!",
            "skip_msg": "Smart move, probably.",
            "backfire_chance": 0.67,
            "win_mult": 2.0,
            "lose_mult": 0.2,
            "win_msg": "⛏️ Actually paid out! Mining profits doubled your money!",
            "lose_msg": "🏃 Mining company vanished overnight. Ponzi scheme confirmed. -80%",
        },
        {
            "name": "🎰 Leverage Trade",
            "desc": "50x leverage on ETH. 'I can feel it going up.'",
            "invest_msg": "You opened a 50x long!",
            "skip_msg": "Leverage dodged.",
            "backfire_chance": 0.67,
            "win_mult": 4.0,
            "lose_mult": 0.05,
            "win_msg": "📈 ETH pumped! 50x leverage = 4x gains! Diamond hands!",
            "lose_msg": "💀 Liquidated in 3 minutes. Lost 95%. Shoulda used a stop-loss.",
        },
        {
            "name": "🌊 NFT Flip",
            "desc": "Floor price dropping. 'Buy the fear, sell the greed'?",
            "invest_msg": "You bought the NFT!",
            "skip_msg": "NFTs avoided.",
            "backfire_chance": 0.67,
            "win_mult": 3.0,
            "lose_mult": 0.15,
            "win_msg": "🎨 Celebrity tweeted about it! 3x flip!",
            "lose_msg": "💩 Collection delisted. Your JPEG is worthless. -85%",
        },
        {
            "name": "🏦 DeFi Yield Farm",
            "desc": "New DeFi protocol offering 500% APY. 'Audited by us.'",
            "invest_msg": "You staked your tokens!",
            "skip_msg": "DeFi dodged.",
            "backfire_chance": 0.67,
            "win_mult": 2.5,
            "lose_mult": 0.1,
            "win_msg": "🌾 Protocol survived! Your yield farming paid off!",
            "lose_msg": "🐛 Smart contract exploit. Funds drained. Welcome to DeFi.",
        },
    ]
    return random.choice(opportunities)

# ============================================================
# PERSONAL QUESTIONS (the mean ones)
# ============================================================
PERSONAL_QUESTIONS = [
    {
        "question": "Do you like gore?",
        "type": "yesno",
        "yes_effect": {"money_mult": 0.5, "message": "Disturbing. Lost half your money. 🩸"},
        "no_effect": {"money_add": 50, "message": "Good. Here's $50 for being normal."},
    },
    {
        "question": "Do you have a girlfriend?",
        "type": "yesno",
        "yes_effect": {"money_add": -50, "message": "Enjoy while it lasts... 💔 -$50"},
        "no_effect": {"money_add": 100, "message": "Here's $100. Buy yourself a dildo. 🍆"},
    },
    {
        "question": "Have you ever cheated on a test?",
        "type": "yesno",
        "yes_effect": {"money_add": 200, "message": "At least you're honest. +$200 🎓"},
        "no_effect": {"money_mult": 0.7, "message": "Liar. Lost 30% of your money. 🤥"},
    },
    {
        "question": "Do you watch anime?",
        "type": "yesno",
        "yes_effect": {"money_mult": 0.6, "message": "Weeb detected. -40% money. 🍥"},
        "no_effect": {"money_add": 75, "message": "Respectable. +$75 💪"},
    },
    {
        "question": "Do you think you're smarter than average?",
        "type": "yesno",
        "yes_effect": {"money_mult": 0.5, "message": "Dunning-Kruger effect. -50% 🧠"},
        "no_effect": {"money_add": 150, "message": "Humble. +$150. (You probably are though) 🤓"},
    },
    {
        "question": "Would you betray your best friend for $1,000,000?",
        "type": "yesno",
        "yes_effect": {"money_mult": 2.0, "message": "Cold blooded. Money doubled. 🐍"},
        "no_effect": {"money_add": -100, "message": "Loyal but poor. -$100 😇"},
    },
    {
        "question": "Do you sleep with socks on?",
        "type": "yesno",
        "yes_effect": {"money_mult": 0.3, "message": "PSYCHOPATH. Lost 70% of your money. 🧦"},
        "no_effect": {"money_add": 25, "message": "Normal human. +$25 🦶"},
    },
    {
        "question": "Is a hot dog a sandwich?",
        "type": "yesno",
        "yes_effect": {"money_add": -200, "message": "WRONG. -$200 🌭"},
        "no_effect": {"money_add": -200, "message": "ALSO WRONG. -$200 🌭"},
    },
    {
        "question": "Have you ever cried during a movie?",
        "type": "yesno",
        "yes_effect": {"money_add": 100, "message": "You have a soul. +$100 😢"},
        "no_effect": {"money_mult": 0.8, "message": "Robot. -20% money. 🤖"},
    },
    {
        "question": "Do you put the toilet seat down?",
        "type": "yesno",
        "yes_effect": {"money_add": 200, "message": "A person of culture. +$200 🚽"},
        "no_effect": {"money_mult": 0.5, "message": "Animal. -50% money. 🐒"},
    },
    {
        "question": "Would you eat a bug for $500?",
        "type": "yesno",
        "yes_effect": {"money_add": 500, "message": "Brave and hungry. Here's $500. 🪲"},
        "no_effect": {"money_add": -50, "message": "Coward. -$50 🐛"},
    },
    {
        "question": "Do you use light mode?",
        "type": "yesno",
        "yes_effect": {"money_mult": 0.1, "message": "PSYCHO. Lost 90% of everything. ☀️💀"},
        "no_effect": {"money_add": 100, "message": "Dark mode supremacy. +$100 🌙"},
    },
]

# ============================================================
# HTTP HANDLER
# ============================================================
class GreedHandler(SimpleHTTPRequestHandler):
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()
    
    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode())
        except:
            return {}
    
    def do_GET(self):
        path = self.path.split('?')[0]
        
        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            with open('index.html', 'rb') as f:
                self.wfile.write(f.read())
            return
        
        if path == '/api/trivia':
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            difficulty = params.get('difficulty', ['easy'])[0]
            count = min(int(params.get('count', ['1'])[0]), 10)
            
            questions = get_trivia_question(difficulty, count)
            self.send_json({"success": True, "questions": questions})
            return
        
        if path == '/api/graph':
            data = generate_graph_data()
            self.send_json({"success": True, "graph": data})
            return
        
        if path == '/api/bitcoin':
            data = generate_bitcoin_opportunity()
            self.send_json({"success": True, "opportunity": data})
            return
        
        if path == '/api/personal':
            q = random.choice(PERSONAL_QUESTIONS)
            self.send_json({"success": True, "question": q})
            return
        
        if path == '/api/leaderboard':
            with lb_lock:
                lb = list(cached_leaderboard[:20])
            if not lb:
                # Fallback to DB if cache empty
                db = load_db()
                lb = db.get("leaderboard", [])[:20]
            self.send_json({"success": True, "leaderboard": lb})
            return
        
        if path == '/api/health':
            with trivia_lock:
                cache_status = {k: len(v) for k, v in trivia_cache.items()}
            self.send_json({
                "status": "ok",
                "trivia_cache": cache_status,
                "uptime": time.time()
            })
            return
        
        # Serve static files
        super().do_GET()
    
    def do_POST(self):
        path = self.path.split('?')[0]
        data = self.read_json()
        
        if path == '/api/signup':
            username = data.get('username', '').strip()
            password = data.get('password', '').strip()
            emoji = data.get('emoji', '😀')
            
            if not username or not password:
                self.send_json({"success": False, "error": "Username and password required"}, 400)
                return
            if len(username) < 2 or len(username) > 20:
                self.send_json({"success": False, "error": "Username must be 2-20 characters"}, 400)
                return
            if len(password) < 3:
                self.send_json({"success": False, "error": "Password must be 3+ characters"}, 400)
                return
            
            db = load_db()
            user = create_user(db, username, password, emoji)
            if not user:
                self.send_json({"success": False, "error": "Username already taken"}, 409)
                return
            
            token = create_session(db, username)
            self.send_json({
                "success": True,
                "token": token,
                "user": {
                    "username": username,
                    "emoji": emoji,
                    "high_score": 0,
                }
            })
            return
        
        if path == '/api/login':
            username = data.get('username', '').strip()
            password = data.get('password', '').strip()
            
            if not username or not password:
                self.send_json({"success": False, "error": "Username and password required"}, 400)
                return
            
            db = load_db()
            user = auth_user(db, username, password)
            if not user:
                self.send_json({"success": False, "error": "Invalid credentials"}, 401)
                return
            
            token = create_session(db, username)
            self.send_json({
                "success": True,
                "token": token,
                "user": {
                    "username": username,
                    "emoji": user.get("emoji", "😀"),
                    "high_score": user.get("high_score", 0),
                    "total_games": user.get("total_games", 0),
                    "total_wins": user.get("total_wins", 0),
                }
            })
            return
        
        if path in ('/api/score', '/api/update_score'):
            token = data.get('token', data.get('session', ''))
            db = load_db()
            username = get_session_user(db, token)
            
            if not username:
                self.send_json({"success": False, "error": "Not logged in"}, 401)
                return
            
            score = data.get('score', 0)
            crashes = data.get('crashes', 0)
            streak = data.get('streak', 0)
            extra_stats = {
                "clicks": data.get('clicks', 0),
                "cashouts": data.get('cashouts', 0),
                "level": data.get('level', 0),
                "play_time": data.get('play_time', 0),
            }
            
            update_score(db, username, score, crashes, streak, extra_stats)
            self.send_json({"success": True, "recorded": score})
            return
        
        if path == '/api/user_stats':
            token = data.get('token', data.get('session', ''))
            db = load_db()
            username = get_session_user(db, token)
            
            if not username:
                self.send_json({"success": False, "error": "Not logged in"}, 401)
                return
            
            stats = get_user_stats(db, username)
            if stats:
                self.send_json({"success": True, "stats": stats})
            else:
                self.send_json({"success": False, "error": "User not found"}, 404)
            return
        
        if path == '/api/check_session':
            token = data.get('token', data.get('session', ''))
            db = load_db()
            username = get_session_user(db, token)
            
            if not username:
                self.send_json({"success": False})
                return
            
            user = get_user(db, username)
            self.send_json({
                "success": True,
                "user": {
                    "username": username,
                    "emoji": user.get("emoji", "😀") if user else "😀",
                    "high_score": user.get("high_score", 0) if user else 0,
                    "total_games": user.get("total_games", 0) if user else 0,
                    "total_wins": user.get("total_wins", 0) if user else 0,
                    "best_streak": user.get("best_streak", 0) if user else 0,
                }
            })
            return
        
        self.send_json({"error": "Not found"}, 404)
    
    def log_message(self, format, *args):
        # Suppress normal request logs, only show errors
        if args and '404' in str(args[0]):
            print(f"[404] {args[0]}")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    # Load caches
    load_trivia_cache()
    
    # Pre-fetch trivia in background
    fetch_thread = threading.Thread(target=prefetch_trivia, daemon=True)
    fetch_thread.start()
    
    # Background refetch thread
    refetch = threading.Thread(target=refetch_thread, daemon=True)
    refetch.start()
    
    # Leaderboard auto-refresh thread (every 1 minute)
    lb_thread = threading.Thread(target=leaderboard_refresh_thread, daemon=True)
    lb_thread.start()
    
    # Initial leaderboard load
    refresh_leaderboard()
    
    server = HTTPServer(("0.0.0.0", PORT), GreedHandler)
    
    print()
    print("=" * 50)
    print("  🎰 The Greed Trial Server v2")
    print(f"  http://localhost:{PORT}")
    print("=" * 50)
    print()
    print(f"  Trivia cache: easy={len(trivia_cache.get('easy', []))}, medium={len(trivia_cache.get('medium', []))}, hard={len(trivia_cache.get('hard', []))}")
    print(f"  Database: {DB_FILE}")
    print(f"  Leaderboard refresh: every {LEADERBOARD_REFRESH_INTERVAL}s")
    print()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...")
        save_trivia_cache()
        server.shutdown()
