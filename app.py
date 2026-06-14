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

# ── Config ─────────────────────────────────────────────────────────────────────
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASSWORD', '')
APP_NAME  = os.getenv('APP_NAME', 'TextBase')
CONSOLE_PASSWORD = os.getenv('CONSOLE_PASSWORD', '')   # optional admin console password
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


# ── Models ─────────────────────────────────────────────────────────────────────

class Account(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    sid        = db.Column(db.String(34), unique=True, nullable=False)
    auth_token = db.Column(db.String(40), nullable=False)
    name       = db.Column(db.String(100), default='My Account')
    status     = db.Column(db.String(20), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages    = db.relationship('Message',        backref='account', lazy='dynamic')
    api_keys    = db.relationship('ApiKey',         backref='account', lazy='dynamic')
    numbers     = db.relationship('PhoneNumber',    backref='account', lazy='dynamic')
    keywords    = db.relationship('Keyword',        backref='account', lazy='dynamic')
    sub_lists   = db.relationship('SubscriberList', backref='account', lazy='dynamic')
    subscribers = db.relationship('Subscriber',     backref='account', lazy='dynamic')
    broadcasts  = db.relationship('Broadcast',      backref='account', lazy='dynamic')

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
    sms_url       = db.Column(db.String(500))   # inbound webhook URL
    status        = db.Column(db.String(20), default='active')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'phone_number': self.number,
            'friendly_name': self.friendly_name or self.number,
            'carrier': self.carrier,
            'carrier_label': CARRIER_GATEWAYS.get(self.carrier, ('Unknown',))[0] if self.carrier else 'Unknown',
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
    status       = db.Column(db.String(20), default='queued')  # queued | sent | failed | undelivered
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


class Keyword(db.Model):
    """
    Keyword auto-responder.
    When an inbound message body matches `keyword`, TextBase immediately
    sends `response` back to the sender via the carrier gateway.

    match_type:
      exact      — body must equal keyword (case-insensitive)
      contains   — body must contain keyword anywhere
      startswith — body must start with keyword
    """
    id          = db.Column(db.Integer, primary_key=True)
    account_id  = db.Column(db.Integer, db.ForeignKey('account.id'))
    keyword     = db.Column(db.String(100), nullable=False)
    response    = db.Column(db.Text, nullable=False)
    match_type  = db.Column(db.String(20), default='exact')   # exact | contains | startswith
    active      = db.Column(db.Boolean, default=True)
    match_count = db.Column(db.Integer, default=0)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def matches(self, body: str) -> bool:
        b = body.strip().lower()
        k = self.keyword.strip().lower()
        if self.match_type == 'exact':
            return b == k
        if self.match_type == 'startswith':
            return b.startswith(k)
        if self.match_type == 'contains':
            return k in b
        return False

    def to_dict(self):
        return {
            'id': self.id,
            'keyword': self.keyword,
            'response': self.response,
            'match_type': self.match_type,
            'active': self.active,
            'match_count': self.match_count,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M'),
        }


class SubscriberList(db.Model):
    """A named group of phone numbers that can receive broadcasts."""
    id          = db.Column(db.Integer, primary_key=True)
    account_id  = db.Column(db.Integer, db.ForeignKey('account.id'))
    name        = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), default='')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    subscribers = db.relationship('Subscriber', backref='subscriber_list',
                                  lazy='dynamic', cascade='all, delete-orphan')
    broadcasts  = db.relationship('Broadcast',  backref='subscriber_list', lazy='dynamic')

    def active_count(self):
        return self.subscribers.filter_by(status='active').count()

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'total': self.subscribers.count(),
            'active': self.active_count(),
            'created_at': self.created_at.strftime('%Y-%m-%d'),
        }


class Subscriber(db.Model):
    """A phone number subscribed to a list."""
    id         = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'))
    list_id    = db.Column(db.Integer, db.ForeignKey('subscriber_list.id'))
    phone      = db.Column(db.String(30), nullable=False)
    name       = db.Column(db.String(100), default='')
    carrier    = db.Column(db.String(30))
    status     = db.Column(db.String(20), default='active')  # active | opted_out
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('list_id', 'phone', name='uq_sub_list_phone'),)

    def carrier_label(self):
        return CARRIER_GATEWAYS.get(self.carrier, ('Unknown',))[0] if self.carrier else '—'

    def to_dict(self):
        return {
            'id': self.id,
            'phone': self.phone,
            'name': self.name,
            'carrier': self.carrier,
            'carrier_label': self.carrier_label(),
            'status': self.status,
            'created_at': self.created_at.strftime('%Y-%m-%d'),
        }


class Broadcast(db.Model):
    """A message sent (or scheduled to send) to all active subscribers in a list."""
    id           = db.Column(db.Integer, primary_key=True)
    account_id   = db.Column(db.Integer, db.ForeignKey('account.id'))
    list_id      = db.Column(db.Integer, db.ForeignKey('subscriber_list.id'))
    name         = db.Column(db.String(200), nullable=False)
    body         = db.Column(db.Text, nullable=False)
    status       = db.Column(db.String(20), default='draft')
    # draft | scheduled | sending | sent | failed
    scheduled_at = db.Column(db.DateTime)          # None → send immediately when triggered
    sent_at      = db.Column(db.DateTime)
    sent_count   = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'body': self.body,
            'list_id': self.list_id,
            'list_name': self.subscriber_list.name if self.subscriber_list else '',
            'status': self.status,
            'scheduled_at': self.scheduled_at.strftime('%Y-%m-%d %H:%M') if self.scheduled_at else None,
            'sent_at': self.sent_at.strftime('%Y-%m-%d %H:%M') if self.sent_at else None,
            'sent_count': self.sent_count,
            'failed_count': self.failed_count,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M'),
        }


def _execute_broadcast(broadcast: 'Broadcast'):
    """Send a broadcast to all active subscribers. Called synchronously."""
    broadcast.status = 'sending'
    db.session.commit()

    subs = Subscriber.query.filter_by(list_id=broadcast.list_id, status='active').all()
    sent = failed = 0
    for sub in subs:
        result = send_sms_gateway(sub.phone, sub.carrier, broadcast.body)
        if result.get('success'):
            sent += 1
        else:
            failed += 1
        # Log each message
        msg = Message(
            sid='SM' + secrets.token_hex(16),
            account_id=broadcast.account_id,
            from_num='Broadcast',
            to_num=sub.phone,
            body=broadcast.body,
            carrier=sub.carrier,
            direction='outbound-api',
            status='sent' if result.get('success') else 'failed',
            error_msg=result.get('error') if not result.get('success') else None,
        )
        db.session.add(msg)

    broadcast.sent_count   = sent
    broadcast.failed_count = failed
    broadcast.status       = 'sent'
    broadcast.sent_at      = datetime.utcnow()
    db.session.commit()
    return sent, failed


# ── Bootstrap: auto-create default account ─────────────────────────────────────

def get_or_create_account():
    acct = Account.query.first()
    if not acct:
        acct = Account(
            sid='AC' + secrets.token_hex(16),
            auth_token=secrets.token_hex(20),
            name='My Account',
        )
        db.session.add(acct)
        # Create a default API key
        key = ApiKey(key='sk_' + secrets.token_urlsafe(32), name='Default Key')
        acct.api_keys.append(key)
        db.session.commit()
    return acct


# ── SMS delivery ───────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    digits = re.sub(r'[^0-9]', '', raw.strip())
    if len(digits) == 10:
        digits = '1' + digits
    return digits


def send_sms_gateway(to: str, carrier: str | None, body: str) -> dict:
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


# ── Auth helpers ───────────────────────────────────────────────────────────────

def require_api_key(f):
    """Authenticate via X-Api-Key header or ?api_key= param."""
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
    """Validate HTTP Basic auth for Twilio-compatible endpoints."""
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


# ── Template context ───────────────────────────────────────────────────────────

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
        'total':       Message.query.filter_by(account_id=acct.id).count(),
        'sent':        Message.query.filter_by(account_id=acct.id, status='sent').count(),
        'failed':      Message.query.filter_by(account_id=acct.id, status='failed').count(),
        'numbers':     PhoneNumber.query.filter_by(account_id=acct.id, status='active').count(),
        'api_keys':    ApiKey.query.filter_by(account_id=acct.id, status='active').count(),
        'subscribers': Subscriber.query.filter_by(account_id=acct.id, status='active').count(),
        'broadcasts':  Broadcast.query.filter_by(account_id=acct.id).count(),
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
# NATIVE REST API  (/api/v1/...)
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

    # Auto-detect carrier from registered numbers
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
# TWILIO-COMPATIBLE API  (/2010-04-01/...)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/2010-04-01/Accounts/<account_sid>/Messages.json', methods=['POST'])
def twilio_send_message(account_sid):
    acct = require_basic_auth(account_sid)
    if not acct:
        return jsonify(code=20003, message='Authenticate', status=401), 401

    to      = (request.form.get('To')   or '').strip()
    from_   = (request.form.get('From') or '').strip()
    body    = (request.form.get('Body') or '').strip()
    carrier = (request.form.get('Carrier') or '').strip() or None  # TextBase extension

    if not to:   return jsonify(code=21201, message="'To' is required", status=400), 400
    if not body: return jsonify(code=21602, message="'Body' is required", status=400), 400

    # Auto-detect carrier from registered numbers
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
# INBOUND WEBHOOK  (/webhook/inbound)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/webhook/inbound', methods=['POST', 'GET'])
def webhook_inbound():
    """Receives inbound SMS from any provider and stores it, then forwards to number's sms_url."""
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

    # Keyword auto-responder
    if acct and body:
        keywords = Keyword.query.filter_by(account_id=acct.id, active=True).all()
        for kw in keywords:
            if kw.matches(body):
                kw.match_count += 1
                db.session.commit()
                # Auto-reply: detect sender's carrier from registered numbers, else best-effort
                sender_num = normalize_phone(from_)
                sender_rec = PhoneNumber.query.filter_by(account_id=acct.id, number=sender_num).first()
                sender_carrier = sender_rec.carrier if sender_rec else None
                send_sms_gateway(from_, sender_carrier, kw.response)
                break   # first match wins

    # Look up number and forward to its webhook
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
# CONSOLE REST ENDPOINTS (used by JS in admin pages)
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


# ── Subscribers console page ───────────────────────────────────────────────────

@app.route('/console/subscribers')
def console_subscribers():
    acct  = get_or_create_account()
    lists = SubscriberList.query.filter_by(account_id=acct.id)\
                                .order_by(SubscriberList.created_at.desc()).all()
    list_id = request.args.get('list', type=int)
    selected_list = SubscriberList.query.get(list_id) if list_id else (lists[0] if lists else None)
    subs = selected_list.subscribers.order_by(Subscriber.created_at.desc()).all() if selected_list else []
    return render_template('console/subscribers.html',
                           lists=lists, selected_list=selected_list,
                           subs=subs, active_nav='subscribers')

@app.route('/console/api/lists', methods=['POST'])
def console_create_list():
    acct = get_or_create_account()
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify(error='Name required'), 400
    lst = SubscriberList(account_id=acct.id, name=name,
                         description=data.get('description', ''))
    db.session.add(lst)
    db.session.commit()
    return jsonify(lst.to_dict()), 201

@app.route('/console/api/lists/<int:lid>', methods=['DELETE'])
def console_delete_list(lid):
    lst = SubscriberList.query.get_or_404(lid)
    db.session.delete(lst)
    db.session.commit()
    return jsonify(message='Deleted')

@app.route('/console/api/lists/<int:lid>/subscribers', methods=['POST'])
def console_add_subscriber(lid):
    lst  = SubscriberList.query.get_or_404(lid)
    data = request.get_json(force=True) or {}
    phone = normalize_phone(data.get('phone', '').strip())
    if not phone:
        return jsonify(error='Phone number required'), 400
    carrier = data.get('carrier', '').strip() or None
    existing = Subscriber.query.filter_by(list_id=lid, phone=phone).first()
    if existing:
        # Re-activate if they were opted out
        if existing.status == 'opted_out':
            existing.status = 'active'
            db.session.commit()
            return jsonify(existing.to_dict())
        return jsonify(error='Already subscribed'), 409
    sub = Subscriber(
        account_id=lst.account_id,
        list_id=lid,
        phone=phone,
        name=data.get('name', '').strip(),
        carrier=carrier,
    )
    db.session.add(sub)
    db.session.commit()
    return jsonify(sub.to_dict()), 201

@app.route('/console/api/lists/<int:lid>/subscribers/import', methods=['POST'])
def console_import_subscribers(lid):
    """Bulk import: expects JSON { subscribers: [{phone, name, carrier}] }"""
    lst  = SubscriberList.query.get_or_404(lid)
    data = request.get_json(force=True) or {}
    rows = data.get('subscribers', [])
    added = skipped = 0
    for row in rows:
        phone = normalize_phone(str(row.get('phone', '')).strip())
        if not phone:
            skipped += 1
            continue
        existing = Subscriber.query.filter_by(list_id=lid, phone=phone).first()
        if existing:
            skipped += 1
            continue
        sub = Subscriber(
            account_id=lst.account_id,
            list_id=lid,
            phone=phone,
            name=str(row.get('name', '')).strip(),
            carrier=str(row.get('carrier', '')).strip() or None,
        )
        db.session.add(sub)
        added += 1
    db.session.commit()
    return jsonify(added=added, skipped=skipped)

@app.route('/console/api/subscribers/<int:sid_>', methods=['DELETE'])
def console_remove_subscriber(sid_):
    sub = Subscriber.query.get_or_404(sid_)
    db.session.delete(sub)
    db.session.commit()
    return jsonify(message='Removed')

@app.route('/console/api/subscribers/<int:sid_>/optout', methods=['POST'])
def console_optout_subscriber(sid_):
    sub = Subscriber.query.get_or_404(sid_)
    sub.status = 'opted_out'
    db.session.commit()
    return jsonify(message='Opted out')


# ── Broadcasts console page ─────────────────────────────────────────────────────

@app.route('/console/broadcasts')
def console_broadcasts():
    acct       = get_or_create_account()
    broadcasts = Broadcast.query.filter_by(account_id=acct.id)\
                                .order_by(Broadcast.created_at.desc()).all()
    lists      = SubscriberList.query.filter_by(account_id=acct.id).all()
    return render_template('console/broadcasts.html',
                           broadcasts=broadcasts, lists=lists, active_nav='broadcasts')

@app.route('/console/api/broadcasts', methods=['POST'])
def console_create_broadcast():
    acct = get_or_create_account()
    data = request.get_json(force=True) or {}
    name    = data.get('name', '').strip()
    body    = data.get('body', '').strip()
    list_id = data.get('list_id')
    sched   = data.get('scheduled_at', '').strip()  # ISO datetime string or empty

    if not name:   return jsonify(error='Name required'), 400
    if not body:   return jsonify(error='Message body required'), 400
    if not list_id: return jsonify(error='Subscriber list required'), 400

    lst = SubscriberList.query.get(list_id)
    if not lst or lst.account_id != acct.id:
        return jsonify(error='List not found'), 404

    scheduled_at = None
    if sched:
        try:
            scheduled_at = datetime.fromisoformat(sched)
        except ValueError:
            return jsonify(error='Invalid scheduled_at format (use ISO 8601)'), 400

    bc = Broadcast(
        account_id=acct.id,
        list_id=list_id,
        name=name,
        body=body,
        status='scheduled' if scheduled_at else 'draft',
        scheduled_at=scheduled_at,
    )
    db.session.add(bc)
    db.session.commit()
    return jsonify(bc.to_dict()), 201

@app.route('/console/api/broadcasts/<int:bid>/send', methods=['POST'])
def console_send_broadcast(bid):
    bc = Broadcast.query.get_or_404(bid)
    if bc.status in ('sent', 'sending'):
        return jsonify(error='Already sent or sending'), 400
    sent, failed = _execute_broadcast(bc)
    return jsonify(status='sent', sent=sent, failed=failed)

@app.route('/console/api/broadcasts/<int:bid>', methods=['DELETE'])
def console_delete_broadcast(bid):
    bc = Broadcast.query.get_or_404(bid)
    if bc.status in ('sent', 'sending'):
        return jsonify(error='Cannot delete a sent broadcast'), 400
    db.session.delete(bc)
    db.session.commit()
    return jsonify(message='Deleted')

@app.route('/run-scheduled')
def run_scheduled_broadcasts():
    """
    Hit this endpoint on a schedule (e.g. UptimeRobot every 5 min, cron-job.org)
    to auto-fire broadcasts whose scheduled_at has passed.
    """
    now        = datetime.utcnow()
    pending    = Broadcast.query.filter(
        Broadcast.status == 'scheduled',
        Broadcast.scheduled_at <= now
    ).all()
    results = []
    for bc in pending:
        sent, failed = _execute_broadcast(bc)
        results.append({'id': bc.id, 'name': bc.name, 'sent': sent, 'failed': failed})
    return jsonify(processed=len(results), broadcasts=results)


# ── Keywords console page ──────────────────────────────────────────────────────

@app.route('/console/keywords')
def console_keywords():
    acct     = get_or_create_account()
    keywords = Keyword.query.filter_by(account_id=acct.id)\
                            .order_by(Keyword.created_at.desc()).all()
    return render_template('console/keywords.html', keywords=keywords, active_nav='keywords')


# ── Keywords console API ────────────────────────────────────────────────────────

@app.route('/console/api/keywords', methods=['POST'])
def console_create_keyword():
    acct = get_or_create_account()
    data = request.get_json(force=True) or {}
    word = data.get('keyword', '').strip().upper()
    resp = data.get('response', '').strip()
    mtype = data.get('match_type', 'exact').strip()
    if not word:
        return jsonify(error='Keyword is required'), 400
    if not resp:
        return jsonify(error='Response message is required'), 400
    if mtype not in ('exact', 'contains', 'startswith'):
        return jsonify(error='Invalid match_type'), 400
    kw = Keyword(
        account_id=acct.id,
        keyword=word,
        response=resp,
        match_type=mtype,
    )
    db.session.add(kw)
    db.session.commit()
    return jsonify(kw.to_dict()), 201

@app.route('/console/api/keywords/<int:kid>', methods=['PUT'])
def console_update_keyword(kid):
    kw   = Keyword.query.get_or_404(kid)
    data = request.get_json(force=True) or {}
    if 'keyword'    in data: kw.keyword    = data['keyword'].strip().upper()
    if 'response'   in data: kw.response   = data['response'].strip()
    if 'match_type' in data: kw.match_type = data['match_type'].strip()
    if 'active'     in data: kw.active     = bool(data['active'])
    db.session.commit()
    return jsonify(kw.to_dict())

@app.route('/console/api/keywords/<int:kid>', methods=['DELETE'])
def console_delete_keyword(kid):
    kw = Keyword.query.get_or_404(kid)
    db.session.delete(kw)
    db.session.commit()
    return jsonify(message='Deleted')

@app.route('/console/api/keywords/<int:kid>/toggle', methods=['POST'])
def console_toggle_keyword(kid):
    kw = Keyword.query.get_or_404(kid)
    kw.active = not kw.active
    db.session.commit()
    return jsonify(active=kw.active)


# ══════════════════════════════════════════════════════════════════════════════
# BOOTSTRAP
# ══════════════════════════════════════════════════════════════════════════════

with app.app_context():
    db.create_all()
    get_or_create_account()   # ensure default account exists

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
