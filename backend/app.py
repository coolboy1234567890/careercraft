from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_bcrypt import Bcrypt
from groq import Groq
from dotenv import load_dotenv
import os
import requests
import stripe
from datetime import date

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

app = Flask(__name__)
CORS(app)
bcrypt = Bcrypt(app)
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

FREE_DAILY_LIMIT = 5

def db_get(table, filters=""):
    res = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{filters}", headers=HEADERS)
    return res.json()

def db_post(table, data):
    res = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data, headers={**HEADERS, "Prefer": "return=representation"})
    return res.json()

def db_patch(table, filters, data):
    res = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{filters}", json=data, headers={**HEADERS, "Prefer": "return=representation"})
    return res.json()

@app.route("/")
def home():
    return jsonify({"message": "CareerCraft API is running! 🚀"})

@app.route("/signup", methods=["POST"])
def signup():
    data = request.json
    email = data.get("email", "").lower().strip()
    password = data.get("password", "")
    promo = data.get("promo_code", "").strip().upper()

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    existing = db_get("users", f"email=eq.{email}&select=id")
    if existing:
        return jsonify({"error": "An account with this email already exists"}), 400

    valid_promo = os.getenv("PROMO_CODE", "").upper()
    plan = "proplus" if promo and promo == valid_promo else "free"

    hashed = bcrypt.generate_password_hash(password).decode("utf-8")
    user = db_post("users", {"email": email, "password": hashed, "plan": plan})

    if not user or "id" not in user[0]:
        return jsonify({"error": "Failed to create account"}), 500

    u = user[0]
    return jsonify({
        "message": "Account created!",
        "user": {"id": u["id"], "email": u["email"], "plan": u["plan"]}
    })

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email", "").lower().strip()
    password = data.get("password", "")

    users = db_get("users", f"email=eq.{email}&select=id,email,password,plan")
    if not users:
        return jsonify({"error": "No account found with this email"}), 404

    user = users[0]
    if not bcrypt.check_password_hash(user["password"], password):
        return jsonify({"error": "Incorrect password"}), 401

    return jsonify({"message": "Logged in!", "user": {"id": user["id"], "email": user["email"], "plan": user["plan"]}})

@app.route("/generate", methods=["POST"])
def generate_resume():
    data = request.json
    user_id = data.get("user_id")
    plan = data.get("plan", "free")

    if plan == "free" and user_id:
        today = str(date.today())
        usage = db_get("usage", f"user_id=eq.{user_id}&date=eq.{today}&select=count,id")

        if usage:
            if usage[0]["count"] >= FREE_DAILY_LIMIT:
                return jsonify({"error": "daily_limit", "message": "You've used all 5 free generations today. Upgrade to Pro for unlimited!"}), 429
            db_patch("usage", f"user_id=eq.{user_id}&date=eq.{today}", {"count": usage[0]["count"] + 1})
        else:
            db_post("usage", {"user_id": user_id, "date": today, "count": 1})

    job_description = data.get("job_description", "")
    experience = data.get("experience", "")
    skills = data.get("skills", "")
    education = data.get("education", "")
    tone = data.get("tone", "professional")
    voice_sample = data.get("voice_sample")
    job_title = data.get("job_title", "Position")

    voice_instruction = ""
    if voice_sample and plan in ["pro", "proplus"]:
        voice_instruction = f"""
        The user has provided a writing sample to match their voice:
        ---
        {voice_sample}
        ---
        Analyze their tone, vocabulary, and sentence style.
        Write the resume and cover letter to sound like THEM, not like a generic AI.
        """

    prompt = f"""
    You are an expert resume writer and career coach with 20 years of experience.

    Job Description:
    {job_description}

    Candidate Information:
    - Experience: {experience}
    - Skills: {skills}
    - Education: {education}
    - Preferred tone: {tone}

    {voice_instruction}

    STRICT RULES — follow every one:
    1. NEVER invent experience, certifications, clearances, or degrees not mentioned by the candidate
    2. Only claim skills the candidate actually listed — if a skill matches the job but wasnt mentioned, use bridging language like "experience applicable to X environments"
    3. Add realistic context — include scale, team size, or impact where logical (e.g. "systems serving millions of users")
    4. Include measurable outcomes where possible (e.g. "reduced deployment time by 30%") but only if they are believable given the candidates background
    5. Use natural varied language — avoid buzzword overload and keyword stuffing
    6. Rotate wording naturally — dont repeat the same phrase in every bullet
    7. Make it sound like a real human wrote it, not an AI
    8. Place ATS keywords naturally throughout, not crammed into every line
    9. Connect the candidates real experience to the job using bridging statements
    10. Cover letter must only reference verified experience from the candidates input

    Please write:
    1. A tailored professional resume using keywords from the job description
    2. A personalized cover letter

    Output ONLY the resume and cover letter. No explanations, no notes, no ATS optimization section.
    """

    chat = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.3-70b-versatile",
    )
    result = chat.choices[0].message.content

    if user_id:
        db_post("resumes", {
            "user_id": user_id,
            "content": result,
            "job_title": job_title
        })

    return jsonify({"result": result})

@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    data = request.json
    plan = data.get("plan")
    user_id = data.get("user_id")
    email = data.get("email")

    price_id = os.getenv("STRIPE_PRO_PRICE_ID") if plan == "pro" else os.getenv("STRIPE_PROPLUS_PRICE_ID")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url="http://127.0.0.1:5500/index.html?success=true&plan=" + plan,
            cancel_url="http://127.0.0.1:5500/index.html?canceled=true",
            metadata={"user_id": user_id, "plan": plan}
        )
        return jsonify({"url": session.url})
    except Exception as e:
        print("STRIPE ERROR:", str(e))
        return jsonify({"error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = stripe.Event.construct_from(request.json, stripe.api_key)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"]["user_id"]
        plan = session["metadata"]["plan"]
        db_patch("users", f"id=eq.{user_id}", {"plan": plan})

    return jsonify({"status": "ok"})

@app.route("/chat", methods=["POST"])
def chat_with_resume():
    data = request.json
    message = data.get("message", "")
    resume = data.get("resume", "")
    
    prompt = f"""
    You are a professional resume coach. The user has just received this resume and cover letter:
    
    ---
    {resume}
    ---
    
    The user is now asking you to help them refine it. Respond helpfully to their request.
    If they ask you to change something, output the FULL updated resume and cover letter.
    If they ask a question, just answer it conversationally.
    
    User message: {message}
    """
    
    chat = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.3-70b-versatile",
    )
    
    return jsonify({"result": chat.choices[0].message.content})

@app.route("/usage", methods=["POST"])
def get_usage():
    data = request.json
    user_id = data.get("user_id")
    today = str(date.today())
    usage = db_get("usage", f"user_id=eq.{user_id}&date=eq.{today}&select=count")
    count = usage[0]["count"] if usage else 0
    return jsonify({"count": count})

@app.route("/score", methods=["POST"])
def score_resume():
    data = request.json
    job_description = data.get("job_description", "")
    resume = data.get("resume", "")

    prompt = f"""
    You are an ATS (Applicant Tracking System) expert.
    
    Analyze this resume against the job description and respond ONLY with a JSON object like this:
    {{
      "score": 82,
      "matched_keywords": ["Python", "React", "Agile"],
      "missing_keywords": ["Docker", "Kubernetes"],
      "tip": "Add Docker experience to significantly boost your match score."
    }}

    Job Description:
    {job_description}

    Resume:
    {resume}

    Rules:
    - Score between 0-100
    - matched_keywords: important keywords found in both
    - missing_keywords: important keywords in job but missing from resume
    - tip: one specific actionable tip
    - Respond with ONLY the JSON, no extra text
    """

    chat = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.3-70b-versatile",
    )

    try:
        import json
        result = json.loads(chat.choices[0].message.content)
    except:
        result = {"score": 75, "matched_keywords": [], "missing_keywords": [], "tip": "Review your resume against the job description."}

    return jsonify(result)


@app.route("/linkedin", methods=["POST"])
def linkedin_bio():
    data = request.json
    plan = data.get("plan", "free")

    if plan != "proplus":
        return jsonify({"error": "Pro+ required"}), 403

    experience = data.get("experience", "")
    skills = data.get("skills", "")
    tone = data.get("tone", "professional")
    voice_sample = data.get("voice_sample", "")

    voice_instruction = f"""
    Match the writing style of this sample:
    ---
    {voice_sample}
    ---
    """ if voice_sample else ""

    prompt = f"""
    You are a LinkedIn profile expert.

    Write a compelling LinkedIn About section for this person:
    - Experience: {experience}
    - Skills: {skills}
    - Tone: {tone}

    {voice_instruction}

    Rules:
    - Maximum 300 words
    - First line must be a hook that grabs attention
    - Write in first person
    - Sound human, not like an AI
    - End with what they are looking for or open to
    - Do NOT use hashtags
    - Output ONLY the bio text, nothing else
    """

    chat = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.3-70b-versatile",
    )

    return jsonify({"result": chat.choices[0].message.content})


@app.route("/improve", methods=["POST"])
def improve_resume():
    data = request.json
    resume = data.get("resume", "")
    job_description = data.get("job_description", "")
    instruction = data.get("instruction", "Improve this resume")

    prompt = f"""
    You are an expert resume coach.

    {"Here is the target job description:" + job_description if job_description else ""}

    Here is the resume to improve:
    {resume}

    User instruction: {instruction}

    Rules:
    - Output the FULL improved resume
    - Keep the same structure but improve the content
    - If a job description is provided, tailor keywords to match it
    - Sound human and natural
    - Do NOT explain what you changed, just output the resume
    """

    chat = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.3-70b-versatile",
    )

    return jsonify({"result": chat.choices[0].message.content})
if __name__ == "__main__":
    app.run(debug=True)
