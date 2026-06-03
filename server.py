from flask import Flask, jsonify, request, send_from_directory, make_response, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone
import requests
import base64
import uuid
import re
import os
import random

app = Flask(__name__, static_folder='.', template_folder='.')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'SUPER_SECRET_SECURITY_PASSPHRASE_KEY_XYZ_123456789')

# ----------------------------------------------------
# IN-MEMORY DATABASES (Volatile - Reset on Render restart)
# ----------------------------------------------------
users_db = {}          # phone -> {password_hash, balance, session_token}
slips_db = {}          # slip_id -> {phone, stake, odds, potential_payout, selection_data, status, timestamp}
transactions_db = []   # List of dicts: [{phone, type, amount, fee, status, timestamp, ref_id}]
stk_mappings = {}      # checkout_id -> {phone, amount}

# ----------------------------------------------------
# API CREDENTIALS & PRODUCTION ENVIRONMENT CONFIG
# ----------------------------------------------------
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"

MPESA_CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET")
MPESA_PASSKEY = os.getenv("MPESA_PASSKEY") # Sandbox Default
MPESA_SHORTCODE = "174379"  # Sandbox Paybill
MPESA_B2C_SHORTCODE = "600000" # Sandbox B2C Payout Shortcode

# Automatically detect Render URL environment variable, fallback to localhost for development
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PUBLIC_URL = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "http://127.0.0.1:5000"

# ----------------------------------------------------
# DECORATORS & CORE SECURITY HELPERS
# ----------------------------------------------------
def get_authenticated_user():
    """Validates secure HTTP-only cookie to fetch active user profile context safely."""
    token = request.cookies.get('session_token')
    if not token:
        return None
    for phone, profile in users_db.items():
        if profile.get('session_token') == token:
            return phone
    return None

def get_mpesa_token():
    """Generates real-time OAuth access tokens required by Safaricom Daraja API."""
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    try:
        res = requests.get(url, auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET), timeout=10)
        return res.json().get("access_token")
    except Exception:
        return None

# ----------------------------------------------------
# STATIC FRONTEND CONTROLLERS
# ----------------------------------------------------
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/style.css')
def styles():
    return send_from_directory('.', 'style.css')

# ----------------------------------------------------
# 1. USER AUTHENTICATION ENDPOINTS
# ----------------------------------------------------
@app.route('/api/auth/signup', methods=['POST'])
def auth_signup():
    data = request.json or {}
    phone = str(data.get('phone', '')).strip()
    password = str(data.get('password', '')).strip()

    if not re.match(r'^254\d{9}$', phone):
        return jsonify({"success": False, "message": "Phone number format must be 254xxxxxxxxx"}), 400
    if len(password) != 4 or not password.isdigit():
        return jsonify({"success": False, "message": "Password must be a strict 4-digit numeric code"}), 400
    if phone in users_db:
        return jsonify({"success": False, "message": "Phone number currently in use!"}), 409

    users_db[phone] = {
        "password_hash": generate_password_hash(password),
        "balance": 0.0, # Rule 2: Force baseline balance to zero
        "session_token": None
    }
    return jsonify({"success": True, "message": "Account created! Proceed to Sign In."})

@app.route('/api/auth/signin', methods=['POST'])
def auth_signin():
    data = request.json or {}
    phone = str(data.get('phone', '')).strip()
    password = str(data.get('password', '')).strip()

    user = users_db.get(phone)
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({"success": False, "message": "Invalid login details"}), 401

    session_token = str(uuid.uuid4())
    users_db[phone]['session_token'] = session_token

    response = make_response(jsonify({"success": True, "message": "Log in successfull!", "balance": user['balance']}))
    response.set_cookie('session_token', session_token, httponly=True, samesite='Strict', secure=True)
    return response

# ----------------------------------------------------
# 3. REAL-TIME ODDS API CAPTURE SYSTEM
# ----------------------------------------------------
@app.route('/api/sports/fixtures', methods=['GET'])
def get_fixtures():
    """Fetches real-time fixtures, including live scores and timers for running games."""
    params = {
        'apiKey': ODDS_API_KEY,
        'daysFrom': 3 # Fetches matches happening within the next 3 days
    }
    try:
        res = requests.get(ODDS_API_URL, params=params, timeout=5)
        
        if res.status_code != 200:
            print(f"Scores API Error Status: {res.status_code}, Response: {res.text}")
        
        if res.status_code == 200:
            raw_fixtures = res.json()
            processed = []
            
            for item in raw_fixtures:
                # 1. Determine if the match is actively live
                is_completed = item.get('completed', False)
                scores = item.get('scores')
                
                # If there are active scores and the match isn't completed, it's live!
                is_live = scores is not None and not is_completed
                
                # 2. Extract live scores if available
                home_score = 0
                away_score = 0
                if scores:
                    for score_record in scores:
                        if score_record['name'] == item['home_team']:
                            home_score = int(score_record['score'])
                        elif score_record['name'] == item['away_team']:
                            away_score = int(score_record['score'])

                # 3. Handle live game match runtime clock (minutes played)
                time_played = None
                if is_live:
                    # Alternative safely tracking via regular mock or api period updates
                    # The Odds API returns full completed logs or periodic data arrays
                    time_played = "Live" # Default status string if exact minute isn't present
                
                processed.append({
                    "id": item['id'],
                    "sport_title": item.get('sport_title', 'Soccer'),
                    "home_team": item['home_team'],
                    "away_team": item['away_team'],
                    "start_time": item['commence_time'],
                    "is_live": is_live,
                    "completed": is_completed,
                    "scores": {
                        "home": home_score,
                        "away": away_score
                    },
                    "time_played": time_played
                })
            return jsonify({"success": True, "fixtures": processed})
            
    except Exception as e:
        print(f"Scores API Connection Exception: {e}")

    # Updated Sandbox fallback showcasing live data configurations
    mock_fixtures = [
        {
            "id": "mock_1", 
            "sport_title": "English Premier League", 
            "home_team": "Manchester City", 
            "away_team": "Arsenal", 
            "start_time": "2026-08-10T19:45:00Z", 
            "is_live": True, 
            "completed": False,
            "scores": {"home": 2, "away": 1}, 
            "time_played": "74'"  # Displaying 74th minute ⏱️
        },
        {
            "id": "mock_2", 
            "sport_title": "La Liga", 
            "home_team": "Real Madrid", 
            "away_team": "Barcelona", 
            "start_time": "2026-08-11T20:00:00Z", 
            "is_live": False, 
            "completed": False,
            "scores": {"home": 0, "away": 0}, 
            "time_played": None
        }    
    ]

    return jsonify({"success": True, "fixtures": mock_fixtures, "note": "Displaying system sandbox matches."})


# ----------------------------------------------------
# 4, 5, 6. CORE SPORTS BET SLIP MECHANICS
# ----------------------------------------------------
@app.route('/api/bet/place', methods=['POST'])
def place_betslip():
    phone = get_authenticated_user()
    if not phone: return jsonify({"success": False, "message": "Failed to verify phone!"}), 401

    data = request.json or {}
    stake = float(data.get('stake', 0))
    total_odds = float(data.get('total_odds', 1.0))
    selections = data.get('selections', [])

    if stake <= 0: return jsonify({"success": False, "message": "Invalid stake amount"}), 400
    if users_db[phone]['balance'] < stake: return jsonify({"success": False, "message": "Insufficient balance"}), 400
    
    # Rule 5: Odds Cap Constraint Verification
    if total_odds > 1000.0:
        return jsonify({"success": False, "message": "Odds limit restricted to 1000 odds!"}), 400

    # Charge balance immediately
    users_db[phone]['balance'] -= stake
    slip_id = str(uuid.uuid4())[:8]
    
    slips_db[slip_id] = {
        "phone": phone, "stake": stake, "odds": total_odds,
        "potential_payout": stake * total_odds, "selections": selections,
        "status": "Active", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    transactions_db.append({
        "phone": phone, "type": "BET_PLACEMENT", "amount": stake, "fee": 0.0,
        "status": "Successful", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "ref_id": slip_id
    })
    return jsonify({"success": True, "message": f"Bet placed successfully! Slip ID: {slip_id}", "new_balance": users_db[phone]['balance']})

@app.route('/api/bet/cancel', methods=['POST'])
def cancel_betslip():
    phone = get_authenticated_user()
    if not phone: return jsonify({"success": False, "message": "Failed to authenticate phone!"}), 401

    data = request.json or {}
    slip_id = data.get('slip_id')
    slip = slips_db.get(slip_id)

    if not slip or slip['phone'] != phone:
        return jsonify({"success": False, "message": "Target betslip structure not found"}), 404
    if slip['status'] != "Active":
        return jsonify({"success": False, "message": f"Betslip is currently marked as: {slip['status']}"}), 400

    # Rule 6: Validate no games inside choice profile have started yet
    for game in slip['selections']:
        commence_time = datetime.fromisoformat(game['start_time'].replace('Z', '+00:00'))
        if datetime.now(timezone.utc) >= commence_time:
            return jsonify({"success": False, "message": "Not available! One or more games have started or expired."}), 400

    # Rule 6: Process cancellation by executing a strict 10% penalty charge
    penalty = slip['stake'] * 0.10
    refund_amount = slip['stake'] - penalty
    
    slip['status'] = "Cancelled"
    users_db[phone]['balance'] += refund_amount

    transactions_db.append({
        "phone": phone, "type": "BET_CANCELLATION", "amount": refund_amount, "fee": penalty,
        "status": "Processed", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "ref_id": slip_id
    })
    return jsonify({"success": True, "message": f"Slip cancelled. Refunded: KSH {refund_amount:.2f} (Charged KSH {penalty:.2f} penalty)", "new_balance": users_db[phone]['balance']})

# ----------------------------------------------------
# 2, 7. MPESA DEPOSIT INFRASTRUCTURE (C2B STK PUSH)
# ----------------------------------------------------
@app.route('/api/wallet/deposit', methods=['POST'])
def mpesa_deposit():
    phone = get_authenticated_user()
    if not phone: return jsonify({"success": False, "message": "Failed to Authenticate phone!"}), 401

    data = request.json or {}
    amount = int(data.get('amount', 0))

    # Rule 5: Core Minimum bounds enforcement
    if amount < 5: 
        return jsonify({"success": False, "message": "Minimum deposit transaction  is ksh 5"}), 400

    token = get_mpesa_token()
    if not token: return jsonify({"success": False, "message": "Failed to communicate to server!"}), 500

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password = base64.b64encode((MPESA_SHORTCODE + MPESA_PASSKEY + timestamp).encode()).decode('utf-8')

    payload = {
        "BusinessShortCode": MPESA_SHORTCODE, "Password": password, "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline", "Amount": amount, "PartyA": phone,
        "PartyB": MPESA_SHORTCODE, "PhoneNumber": phone,
        "CallBackURL": f"{PUBLIC_URL}/api/mpesa/deposit-callback",
        "AccountReference": "PrimesBetting", "TransactionDesc": "Deposit Wallet Funding"
    }

    try:
        res = requests.post("https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest", json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        res_data = res.json()
        if res_data.get("ResponseCode") == "0":
            checkout_id = res_data.get("CheckoutRequestID")
            stk_mappings[checkout_id] = {"phone": phone, "amount": amount}
            return jsonify({"success": True, "message": "STK validation instruction pushed to phone. Complete input step."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Connection tracking exception context: {str(e)}"}), 500
    return jsonify({"success": False, "message": "Transaction rejection received by Safaricom platform"}), 400

@app.route('/api/mpesa/deposit-callback', methods=['POST'])
def mpesa_deposit_callback():
    """Asynchronous secure incoming webhook delivering final execution details directly from Render."""
    body = request.json.get('Body', {})
    stk_callback = body.get('stkCallback', {})
    res_code = stk_callback.get('ResultCode')
    checkout_id = stk_callback.get('CheckoutRequestID')

    mapping = stk_mappings.pop(checkout_id, None)
    if mapping and res_code == 0:
        phone = mapping['phone']
        amount = float(mapping['amount'])
        
        # Rule 2: Exclusively updates user balance via verified M-Pesa callbacks
        users_db[phone]['balance'] += amount
        transactions_db.append({
            "phone": phone, "type": "MPESA_DEPOSIT", "amount": amount, "fee": 0.0,
            "status": "Completed", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "ref_id": checkout_id
        })
    return jsonify({"ResultCode": 0, "ResultDesc": "Callback processed and stored successfully."})

# ----------------------------------------------------
# 4, 8. MPESA SECURE WITHDRAWAL SYSTEM (B2C PAYOUTS)
# ----------------------------------------------------
@app.route('/api/wallet/withdraw', methods=['POST'])
def mpesa_withdraw():
    phone = get_authenticated_user()
    if not phone: return jsonify({"success": False, "message": "Failed to verify phone!"}), 401

    data = request.json or {}
    amount = float(data.get('amount', 0))
    password = str(data.get('password', '')).strip()

    # Rule 8: Password checkpoint authentication verification before any withdrawal
    if not check_password_hash(users_db[phone]['password_hash'], password):
        return jsonify({"success": False, "message": "Invalid credentials!"}), 403

    # Rule 5: Baseline bounds limit validation
    if amount < 5: 
        return jsonify({"success": False, "message": "Minimum withdrawal transaction is KSH 5"}), 400

    # Rule 4: Compute accurate 5% withdrawal fee deductions
    withdrawal_fee = amount * 0.05
    total_deduction = amount + withdrawal_fee

    if users_db[phone]['balance'] < total_deduction:
        return jsonify({"success": False, "message": f"Insufficient balance. Required amount with processing fee: KSH {total_deduction:.2f}"}), 400

    token = get_mpesa_token()
    if not token: return jsonify({"success": False, "message": "Failed to communicate to server!"}), 500

    # Debit balance safely
    users_db[phone]['balance'] -= total_deduction
    tx_ref = str(uuid.uuid4())[:8]

    payload = {
        "InitiatorName": "testapi",
        "SecurityCredential": "YOUR_ENCRYPTED_B2C_PASSWORD_CREDENTIAL", 
        "CommandID": "BusinessPayment",
        "Amount": int(amount),
        "PartyA": MPESA_B2C_SHORTCODE,
        "PartyB": phone,
        "Remarks": "Wallet Payout Fulfillment",
        "QueueTimeOutURL": f"{PUBLIC_URL}/api/mpesa/payout-timeout",
        "ResultURL": f"{PUBLIC_URL}/api/mpesa/payout-callback",
        "Occasion": "Withdrawal"
    }

    transactions_db.append({
        "phone": phone, "type": "MPESA_WITHDRAWAL", "amount": amount, "fee": withdrawal_fee,
        "status": "Pending_Callback", "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "ref_id": tx_ref
    })

    try:
        requests.post("https://sandbox.safaricom.co.ke/mpesa/b2c/v1/paymentrequest", json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        return jsonify({"success": True, "message": f"Withdrawal request processed. KSH {withdrawal_fee:.2f} fee deducted.", "new_balance": users_db[phone]['balance']})
    except Exception:
        # Emergency rollback if API pipeline breaks down completely
        users_db[phone]['balance'] += total_deduction
        transactions_db[-1]['status'] = "Failed_API_Disconnection"
        return jsonify({"success": False, "message": "Downstream execution channel error."}), 500

@app.route('/api/mpesa/payout-callback', methods=['POST'])
def mpesa_payout_callback():
    return jsonify({"ResultCode": 0, "ResultDesc": "B2C Callback settled successfully."})

@app.route('/api/mpesa/payout-timeout', methods=['POST'])
def mpesa_payout_timeout():
    return jsonify({"ResultCode": 0, "ResultDesc": "Timeout acknowledged."})

# ----------------------------------------------------
# 9. USER TRANSACTION & HISTORY DATA ENDPOINTS
# ----------------------------------------------------
@app.route('/api/user/dashboard', methods=['GET'])
def get_user_dashboard():
    phone = get_authenticated_user()
    if not phone: return jsonify({"success": False, "message": "Verification failed!"}), 401

    user_slips = {sid: slip for sid, slip in slips_db.items() if slip['phone'] == phone}
    user_txs = [tx for tx in transactions_db if tx['phone'] == phone]

    return jsonify({
        "success": True,
        "balance": users_db[phone]['balance'],
        "phone": phone,
        "bet_history": user_slips,
        "transaction_history": user_txs
    })

@app.route('/api/auth/signout', methods=['POST'])
def auth_signout():
    phone = get_authenticated_user()
    if phone in users_db:
        users_db[phone]['session_token'] = None
    response = make_response(jsonify({"success": True, "message": "Logged out successfully"}))
    response.delete_cookie('session_token')
    return response

#-----------------------------
#Aviator functionality
#-----------------------------

@app.route('/api/aviator/generate-crash', methods=['GET'])
def generate_crash_point():
    """
    Generates highly volatile and unpredictable crash points matching realistic sports multiplier models.
    """
    # 1. 3% probability threshold for instant house crash at 1.00x
    if random.random() < 0.03:
        crash_point = 1.00
    else:
        # 2. Mathematical inversion logic simulating real high-adrenaline volatility curves
        e = random.random()
        crash_point = 1.01 + (0.99 / (1.0001 - e)) * 0.02
        
        # Upper ceiling cap out limit for script loop safety
        if crash_point > 150.00:
            crash_point = random.uniform(50.00, 150.00)
            
    return jsonify({
        "status": "success",
        "crash_point": round(crash_point, 2)
    })


# ----------------------------------------------------
# PRODUCTION EXECUTION LAYER FOR CLOUD ROUTING
# ----------------------------------------------------
if __name__ == '__main__':
    # Render binds automatically to dynamic port states on public host interfaces (0.0.0.0)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
