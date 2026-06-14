"""
TextBase — Self-hosted, Twilio-compatible SMS API Platform
Free forever. Zero per-message cost. No vendor lock-in.
Delivers via email-to-SMS carrier gateways (no API keys needed).

Twilio-compatible endpoint:
  POST /2010-04-01/Accounts/{AccountSid}/Messages.json
  Auth: HTTP Basic (AccountSid : AuthToken)

Native API:
  POST /api/v1/messages
  Auth: X-Api-Key header
"""
import os, smtplib, secrets, base64, re, json
from datetime import datetime
from functools import wraps
from email.mime.text import MIMEText

from flask import Flask, render_template, request, jsonify, redirect, url_for, g, abort, Response
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///textbase.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
db = SQLAlchemy(app)

# ── Config ──────────────────────────────────────────────────────────────────────────────────
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASSWORD', '')
APP_NAME  = os.getenv('APP_NAME', 'TextBase')
CONSOLE_PASSWORD = os.getenv('CONSOLE_PASSWORD', '')
MODE      = 'live' if (SMTP_USER and SMTP_PASS) else 'sandbox'

CARRIER_GATEWAYS = {
    'att':        ('AT&T',               'txt.att.net'),
    'verizon':    ('Verizon',            'vtext.com'),
    'tmobile':    ('T-Mobile',           'tmomail.net'),
    'sprint':     ('Sprint',             'messaging.sprintpcs.com'),
    'boost':      ('Boost Mobile',       'sms.myboostmobile.com'),
    'cricket':    ('Cricket Wireless',   'sms.cricketwireless.net'),
    'metro':      ('Metro by T-Mobile',  'mymetropcs.com'),
    'uscellular': ('US Cellular',        'email.uscc.net'),
    'virgin':     ('Virgin Mobile',      'vmobl.com'),
    'mint':       ('Mint Mobile',        'tmomail.net'),
    'google_fi':  ('Google Fi',          'msg.fi.google.com'),
    'consumer':   ('Consumer Cellular',  'mailmymobile.net'),
    'tracfone':   ('Tracfone',           'mmst5.tracfone.com'),
    'republic':   ('Republic Wireless',  'text.republicwireless.com'),
    'xfinity':    ('Xfinity Mobile',     'vtext.com'),
}


# ── Models ──────────────────────────────────────────────────────────────────────────────────

class Account(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    sid        = db.Column(db.String(34), unique=True, nullable=False)
    auth_token = db.Column(db.String(40), nullable=False)
    name       = db.Column(db.String(100), default='My Account')
    status     = db.Column(db.String(20), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship('Message',     backref='account', lazy='dynamic')
    api_keys = db.relationship('ApiKey',      backref='account', lazy='dynamic')
    numbers  = db.relationship('PhoneNumber', backref='account', lazy='dynamic')

    def to_dict(self):
        return {
            'sid': self.sid,
            'friendly_name': self.name,
            'status': self.status,
            'type': 'Full',
            'date_created': self.created_at.strftime('%a, %d %b %Y %H:%M:%S +0000'),
            'uri': f'/2010-04-01/Accounts/{self.sid}.json',
        }


class PhoneNumber(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    account_id    = db.Column(db.Integer, db.ForeignKey('account.id'))
    number        = db.Column(db.String(30), unique=True, nullable=False)
    friendly_name = db.Column(db.String(100))
    carrier       = db.Column(db.String(30))
    sms_url       = db.Column(db.String(500))
    status        = db.Column(db.String(20), default='active')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def carrier_label(self):
        return CARRIER_GATEWAYS.get(self.carrier, ('Unknown',))[0] if self.carrier else 'Unknown'

    def to_dict(self):
        return {
            'phone_number': self.number,
            'friendly_name': self.friendly_name or self.number,
            'carrier': self.carrier,
            'carrier_label': self.carrier_label(),
            'sms_url': self.sms_url,
            'status': self.status,
        }


class Message(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    sid          = db.Column(db.String(34), unique=True, nullable=False)
    account_id   = db.Column(db.Integer, db.ForeignKey('account.id'))
    from_num     = db.Column(db.String(30))
    to_num       = db.Column(db.String(30))
    body         = db.Column(db.Text)
    carrier      = db.Column(db.String(30))
    direction    = db.Column(db.String(20), default='outbound-api')
    status       = db.Column(db.String(20), default='queued')
    error_code   = db.Column(db.String(10))
    error_msg    = db.Column(db.Text)
    num_segments = db.Column(db.Integer, default=1)
    sent_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        acct_sid = self.account.sid if self.account else ''
        return {
            'sid': self.sid,
            'account_sid': acct_sid,
            'from': self.from_num,
            'to': self.to_num,
            'body': self.body,
            'status': self.status,
            'direction': self.direction,
            'num_segments': self.num_segments,
            'price': '0.00000',
            'price_unit': 'USD',
            'error_code': self.error_code,
            'error_message': self.error_msg,
            'date_created': self.sent_at.strftime('%a, %d %b %Y %H:%M:%S +0000'),
            'date_sent': self.sent_at.strftime('%a, %d %b %Y %H:%M:%S +0000') if self.status == 'sent' else None,
            'date_updated': self.sent_at.strftime('%a, %d %b %Y %H:%M:%S +0000'),
            'uri': f'/2010-04-01/Accounts/{acct_sid}/Messages/{self.sid}.json',
            'subresource_uris': {},
        }


class ApiKey(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'))
    key        = db.Column(db.String(80), unique=True, nullable=False)
    name       = db.Column(db.String(100), default='Default Key')
    status     = db.Column(db.String(20), default='active')
    last_used  = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ── Bootstrap ─────────────────────────────────────────────────────────────────────────────────
from typing import Optional

def get_or_create_account():
    acct = Account.query.first()
    if not acct:
        acct = Account(
            sid='AC' + secrets.token_hex(16),
            auth_token=secrets.token_hex(20),
            name='My Account',
        )
        db.session.add(acct)
        key = ApiKey(key='sk_' + secrets.token_urlsafe(32), name='Default Key')
        acct.api_keys.append(key)
        db.session.commit()
    return acct


# ── SMS delivery ─────────────────────────────────────────────────────────────────────────────────
def normalize_phone(raw: str) -> str:
    digits = re.sub(r'[^0-9]', '', raw.strip())
    if len(digits) == 10:
        digits = '1' + digits
    return digits


def send_sms_gateway(to: str, carrier: Optional[str], body: str) -> dict:
    digits  = normalize_phone(to)
    gateway = CARRIER_GATEWAYS.get(carrier or '')[1] if carrier else None

    if not gateway:
        print(f'[SANDBOX] → {to} (no carrier): {body}')
        return {'success': True, 'sandbox': True, 'note': 'no gateway — carrier unknown'}

    to_addr = f'{digits}@{gateway}'

    if not SMTP_USER or not SMTP_PASS:
        print(f'[SANDBOX] email→SMS to {to_addr}: {body}')
        return {'success': True, 'sandbox': True, 'to': to_addr}

    msg = MIMEText(body)
    msg['From']    = SMTP_USER
    msg['To']      = to_addr
    msg['Subject'] = ''

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.ehlo(); s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return {'success': True, 'to': to_addr}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ── Auth helpers ────────────────────────────────────────────────────────────────────────────────
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key_val = (request.headers.get('X-Api-Key') or
                   request.args.get('api_key') or
                   request.headers.get('Authorization', '').replace('Bearer ', ''))
        if not key_val:
            return jsonify(code=20003, message='Authenticate', status=401), 401
        key = ApiKey.query.filter_by(key=key_val, status='active').first()
        if not key:
            return jsonify(code=20003, message='Invalid API key', status=401), 401
        key.last_used = datetime.utcnow()
        db.session.commit()
        g.account = key.account
        return f(*args, **kwargs)
    return decorated


def require_basic_auth(account_sid):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return None
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        sid, token = decoded.split(':', 1)
    except Exception:
        return None
    acct = Account.query.filter_by(sid=account_sid).first()
    if not acct or acct.sid != sid or acct.auth_token != token:
        return None
    return acct


# ── Template context ─────────────────────────────────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    acct = Account.query.first()
    carrier_list = [(k, v[0]) for k, v in CARRIER_GATEWAYS.items()]
    return dict(
        app_name=APP_NAME,
        mode=MODE,
        account=acct,
        carrier_list=carrier_list,
        smtp_configured=bool(SMTP_USER and SMTP_PASS),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN CONSOLE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return redirect(url_for('console_dashboard'))

@app.route('/console')
def console_dashboard():
    acct = get_or_create_account()
    stats = {
        'total':     Message.query.filter_by(account_id=acct.id).count(),
        'sent':      Message.query.filter_by(account_id=acct.id, status='sent').count(),
        'failed':    Message.query.filter_by(account_id=acct.id, status='failed').count(),
        'numbers':   PhoneNumber.query.filter_by(account_id=acct.id, status='active').count(),
        'api_keys':  ApiKey.query.filter_by(account_id=acct.id, status='active').count(),
    }
    recent = Message.query.filter_by(account_id=acct.id)\
                          .order_by(Message.sent_at.desc()).limit(10).all()
    return render_template('console/dashboard.html', stats=stats, recent=recent, active_nav='dashboard')

@app.route('/console/messages')
def console_messages():
    acct  = get_or_create_account()
    status_f = request.args.get('status', 'all')
    dir_f    = request.args.get('direction', 'all')
    q = Message.query.filter_by(account_id=acct.id)
    if status_f != 'all':   q = q.filter_by(status=status_f)
    if dir_f    != 'all':   q = q.filter_by(direction=dir_f)
    messages = q.order_by(Message.sent_at.desc()).limit(200).all()
    return render_template('console/messages.html', messages=messages,
                           status_f=status_f, dir_f=dir_f, active_nav='messages')

@app.route('/console/numbers')
def console_numbers():
    acct    = get_or_create_account()
    numbers = PhoneNumber.query.filter_by(account_id=acct.id)\
                               .order_by(PhoneNumber.created_at.desc()).all()
    return render_template('console/numbers.html', numbers=numbers, active_nav='numbers')

@app.route('/console/api-keys')
def console_api_keys():
    acct = get_or_create_account()
    keys = ApiKey.query.filter_by(account_id=acct.id)\
                       .order_by(ApiKey.created_at.desc()).all()
    return render_template('console/api_keys.html', keys=keys, active_nav='api_keys')

@app.route('/console/settings')
def console_settings():
    acct = get_or_create_account()
    cfg = {
        'mode':             MODE,
        'smtp_configured':  bool(SMTP_USER and SMTP_PASS),
        'smtp_host':        SMTP_HOST,
        'smtp_user':        SMTP_USER,
        'account_sid':      acct.sid,
        'auth_token':       acct.auth_token,
    }
    first_key = ApiKey.query.filter_by(account_id=acct.id, status='active').first()
    cfg['api_key'] = first_key.key if first_key else ''
    return render_template('console/settings.html', cfg=cfg, active_nav='settings')


# ══════════════════════════════════════════════════════════════════════════════
# NATIVE REST API
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/v1/messages', methods=['POST'])
@require_api_key
def api_send_message():
    data    = request.get_json(force=True) or {}
    to      = data.get('to', '').strip()
    from_   = data.get('from', '').strip() or 'TextBase'
    body    = data.get('body', '').strip()
    carrier = data.get('carrier', '').strip() or None

    if not to:   return jsonify(error='to is required'), 400
    if not body: return jsonify(error='body is required'), 400
    if len(body) > 1600: return jsonify(error='body exceeds 1600 characters'), 400

    if not carrier:
        num = PhoneNumber.query.filter_by(account_id=g.account.id, number=normalize_phone(to)).first()
        if num and num.carrier:
            carrier = num.carrier

    msg = Message(
        sid='SM' + secrets.token_hex(16),
        account_id=g.account.id,
        from_num=from_,
        to_num=normalize_phone(to),
        body=body,
        carrier=carrier,
        direction='outbound-api',
        status='queued',
        num_segments=max(1, (len(body) + 159) // 160),
    )
    db.session.add(msg)
    db.session.flush()

    result = send_sms_gateway(to, carrier, body)
    msg.status = 'sent' if result.get('success') else 'failed'
    if not result.get('success'):
        msg.error_msg = result.get('error', 'Unknown error')
        msg.error_code = '30007'
    db.session.commit()

    return jsonify(msg.to_dict()), 201 if msg.status == 'sent' else 200

@app.route('/api/v1/messages', methods=['GET'])
@require_api_key
def api_list_messages():
    msgs = Message.query.filter_by(account_id=g.account.id)\
                        .order_by(Message.sent_at.desc()).limit(100).all()
    return jsonify(messages=[m.to_dict() for m in msgs], count=len(msgs))

@app.route('/api/v1/messages/<sid>', methods=['GET'])
@require_api_key
def api_get_message(sid):
    msg = Message.query.filter_by(sid=sid, account_id=g.account.id).first_or_404()
    return jsonify(msg.to_dict())

@app.route('/api/v1/numbers', methods=['GET'])
@require_api_key
def api_list_numbers():
    nums = PhoneNumber.query.filter_by(account_id=g.account.id).all()
    return jsonify(numbers=[n.to_dict() for n in nums])

@app.route('/api/v1/numbers', methods=['POST'])
@require_api_key
def api_add_number():
    data    = request.get_json(force=True) or {}
    number  = data.get('phone_number', '').strip()
    carrier = data.get('carrier', '').strip() or None
    if not number:
        return jsonify(error='phone_number required'), 400
    digits = normalize_phone(number)
    existing = PhoneNumber.query.filter_by(number=digits).first()
    if existing:
        return jsonify(error='Number already registered'), 409
    num = PhoneNumber(
        account_id=g.account.id,
        number=digits,
        friendly_name=data.get('friendly_name', digits),
        carrier=carrier,
        sms_url=data.get('sms_url'),
    )
    db.session.add(num)
    db.session.commit()
    return jsonify(num.to_dict()), 201

@app.route('/api/v1/account', methods=['GET'])
@require_api_key
def api_get_account():
    return jsonify(g.account.to_dict())

@app.route('/api/v1/keys', methods=['POST'])
@require_api_key
def api_create_key():
    data = request.get_json(force=True) or {}
    key  = ApiKey(
        account_id=g.account.id,
        key='sk_' + secrets.token_urlsafe(32),
        name=data.get('name', 'New Key'),
    )
    db.session.add(key)
    db.session.commit()
    return jsonify(key=key.key, name=key.name, created_at=key.created_at.isoformat()), 201

@app.route('/api/v1/keys/<int:kid>', methods=['DELETE'])
@require_api_key
def api_revoke_key(kid):
    key = ApiKey.query.filter_by(id=kid, account_id=g.account.id).first_or_404()
    key.status = 'revoked'
    db.session.commit()
    return jsonify(message='Revoked')


# ══════════════════════════════════════════════════════════════════════════════
# TWILIO-COMPATIBLE API
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/2010-04-01/Accounts/<account_sid>/Messages.json', methods=['POST'])
def twilio_send_message(account_sid):
    acct = require_basic_auth(account_sid)
    if not acct:
        return jsonify(code=20003, message='Authenticate', status=401), 401

    to      = (request.form.get('To')   or '').strip()
    from_   = (request.form.get('From') or '').strip()
    body    = (request.form.get('Body') or '').strip()
    carrier = (request.form.get('Carrier') or '').strip() or None

    if not to:   return jsonify(code=21201, message="'To' is required", status=400), 400
    if not body: return jsonify(code=21602, message="'Body' is required", status=400), 400

    if not carrier:
        num = PhoneNumber.query.filter_by(account_id=acct.id, number=normalize_phone(to)).first()
        if num and num.carrier:
            carrier = num.carrier

    msg = Message(
        sid='SM' + secrets.token_hex(16),
        account_id=acct.id,
        from_num=from_ or 'TextBase',
        to_num=normalize_phone(to),
        body=body,
        carrier=carrier,
        direction='outbound-api',
        status='queued',
        num_segments=max(1, (len(body) + 159) // 160),
    )
    db.session.add(msg)
    db.session.flush()

    result = send_sms_gateway(to, carrier, body)
    msg.status = 'sent' if result.get('success') else 'failed'
    if not result.get('success'):
        msg.error_msg  = result.get('error', 'Delivery failed')
        msg.error_code = '30007'
    db.session.commit()
    return jsonify(msg.to_dict()), 201


@app.route('/2010-04-01/Accounts/<account_sid>/Messages.json', methods=['GET'])
def twilio_list_messages(account_sid):
    acct = require_basic_auth(account_sid)
    if not acct:
        return jsonify(code=20003, message='Authenticate', status=401), 401
    msgs  = Message.query.filter_by(account_id=acct.id)\
                         .order_by(Message.sent_at.desc()).limit(50).all()
    return jsonify(messages=[m.to_dict() for m in msgs],
                   page=0, page_size=50, start=0, end=len(msgs),
                   first_page_uri=f'/2010-04-01/Accounts/{account_sid}/Messages.json',
                   uri=f'/2010-04-01/Accounts/{account_sid}/Messages.json')


@app.route('/2010-04-01/Accounts/<account_sid>/Messages/<message_sid>.json', methods=['GET'])
def twilio_get_message(account_sid, message_sid):
    acct = require_basic_auth(account_sid)
    if not acct:
        return jsonify(code=20003, message='Authenticate', status=401), 401
    msg = Message.query.filter_by(sid=message_sid, account_id=acct.id).first_or_404()
    return jsonify(msg.to_dict())


@app.route('/2010-04-01/Accounts/<account_sid>.json', methods=['GET'])
def twilio_get_account(account_sid):
    acct = require_basic_auth(account_sid)
    if not acct:
        return jsonify(code=20003, message='Authenticate', status=401), 401
    return jsonify(acct.to_dict())


# ══════════════════════════════════════════════════════════════════════════════
# INBOUND WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/webhook/inbound', methods=['POST', 'GET'])
def webhook_inbound():
    if request.is_json:
        d     = request.get_json() or {}
        from_ = d.get('From', d.get('from', ''))
        body  = d.get('Body', d.get('text', ''))
    else:
        from_ = request.form.get('From', '')
        body  = request.form.get('Body', '')

    if not from_:
        return Response(status=200)

    acct = Account.query.first()
    if acct:
        msg = Message(
            sid='SM' + secrets.token_hex(16),
            account_id=acct.id,
            from_num=normalize_phone(from_),
            to_num='textbase',
            body=body,
            direction='inbound',
            status='received',
        )
        db.session.add(msg)
        db.session.commit()

    to_num = request.form.get('To', '') or request.args.get('To', '')
    num = PhoneNumber.query.filter_by(number=normalize_phone(to_num)).first() if to_num else None
    if num and num.sms_url:
        try:
            import urllib.request
            data = f'From={from_}&To={to_num}&Body={body}'.encode()
            urllib.request.urlopen(urllib.request.Request(num.sms_url, data=data), timeout=5)
        except Exception:
            pass

    return Response(status=200)


# ══════════════════════════════════════════════════════════════════════════════
# CONSOLE REST ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/console/api/numbers', methods=['POST'])
def console_add_number():
    acct = get_or_create_account()
    data = request.get_json(force=True) or {}
    num_raw = data.get('number', '').strip()
    if not num_raw:
        return jsonify(error='Number required'), 400
    digits = normalize_phone(num_raw)
    existing = PhoneNumber.query.filter_by(number=digits).first()
    if existing:
        return jsonify(error='Already registered'), 409
    pn = PhoneNumber(
        account_id=acct.id,
        number=digits,
        friendly_name=data.get('friendly_name', digits),
        carrier=data.get('carrier') or None,
        sms_url=data.get('sms_url') or None,
    )
    db.session.add(pn)
    db.session.commit()
    return jsonify(message='Added', number=pn.to_dict()), 201

@app.route('/console/api/numbers/<int:nid>/carrier', methods=['POST'])
def console_set_carrier(nid):
    num     = PhoneNumber.query.get_or_404(nid)
    data    = request.get_json(force=True) or {}
    carrier = data.get('carrier', '').strip()
    if carrier and carrier not in CARRIER_GATEWAYS:
        return jsonify(error='Unknown carrier'), 400
    num.carrier = carrier or None
    db.session.commit()
    return jsonify(message='Updated')

@app.route('/console/api/numbers/<int:nid>', methods=['DELETE'])
def console_delete_number(nid):
    num = PhoneNumber.query.get_or_404(nid)
    db.session.delete(num)
    db.session.commit()
    return jsonify(message='Deleted')

@app.route('/console/api/keys', methods=['POST'])
def console_create_key():
    acct = get_or_create_account()
    data = request.get_json(force=True) or {}
    key  = ApiKey(
        account_id=acct.id,
        key='sk_' + secrets.token_urlsafe(32),
        name=data.get('name', 'New Key'),
    )
    db.session.add(key)
    db.session.commit()
    return jsonify(key=key.key, name=key.name, id=key.id), 201

@app.route('/console/api/keys/<int:kid>', methods=['DELETE'])
def console_revoke_key(kid):
    key = ApiKey.query.get_or_404(kid)
    key.status = 'revoked'
    db.session.commit()
    return jsonify(message='Revoked')

@app.route('/console/api/send-test', methods=['POST'])
def console_send_test():
    acct = get_or_create_account()
    data = request.get_json(force=True) or {}
    to   = data.get('to', '').strip()
    body = data.get('body', 'Test message from TextBase!').strip()
    carrier = data.get('carrier', '').strip() or None

    if not to:
        return jsonify(error='Recipient number required'), 400

    msg = Message(
        sid='SM' + secrets.token_hex(16),
        account_id=acct.id,
        from_num='TextBase',
        to_num=normalize_phone(to),
        body=body,
        carrier=carrier,
        direction='outbound-api',
        status='queued',
    )
    db.session.add(msg)
    db.session.flush()

    result = send_sms_gateway(to, carrier, body)
    msg.status = 'sent' if result.get('success') else 'failed'
    if not result.get('success'):
        msg.error_msg = result.get('error')
    db.session.commit()
    return jsonify(status=msg.status, sandbox=result.get('sandbox', False),
                   error=result.get('error'))


# ══════════════════════════════════════════════════════════════════════════════
# BOOTSTRAP
# ══════════════════════════════════════════════════════════════════════════════

with app.app_context():
    db.create_all()
    get_or_create_account()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
