"""Operations Timeline: Freedom job mirror plus local callback state."""
from __future__ import annotations
import os
import re
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import psycopg
from psycopg.rows import dict_row
from flask import Flask, Response, abort, jsonify, redirect, render_template, url_for, request
from google.cloud import storage
from board_auth import init_board_auth

app = Flask(__name__)
init_board_auth(app)

# Whitelisted timeline definitions: never interpolate URL-supplied SQL.
TIMELINES = {
    'prebooked': dict(title='Prebooked Pickups', view='public.v_timeline_prebooked',
                      future_minutes=240, past_minutes=35, stale_hours=24),
    'delivery': dict(title='Delivery Deadlines', view='public.v_timeline_delivery',
                     future_minutes=240, past_minutes=35, stale_hours=24),
    'callback': dict(title='Callbacks', view='public.v_timeline_callback',
                     future_minutes=60, past_minutes=35, stale_hours=24),
}

def db_connect():
    url = os.environ.get('DATABASE_URL')
    if url:
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=8)
    return psycopg.connect(host=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
                           dbname=os.getenv('DB_NAME','operations'),
                           user=os.getenv('DB_USER','OpsBot_User'),
                           password=os.environ['DB_PASSWORD'],
                           row_factory=dict_row, connect_timeout=8)

def json_rows(rows):
    return [dict(r) for r in rows]

@app.get('/')
def board():
    return render_template('timeline.html', timelines=TIMELINES, selected='prebooked')

@app.get('/board/<key>')
def named_board(key):
    if key not in TIMELINES: abort(404)
    return render_template('timeline.html', timelines=TIMELINES, selected=key)

@app.get('/api/timeline/<key>')
def timeline_data(key):
    config = TIMELINES.get(key)
    if not config: abort(404)
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(hours=config['stale_hours'])
    latest = now + timedelta(minutes=config['future_minutes'])
    # View name comes exclusively from the source-code whitelist above.
    query = (f"SELECT v.* FROM {config['view']} AS v "
             "WHERE v.critical_at >= %s AND v.critical_at <= %s ")
    params = [earliest, latest]
    if key in TIMELINE_REMOVAL_REASONS:
        query += ("AND NOT EXISTS (SELECT 1 FROM public.timeline_job_suppressions s "
                  "WHERE s.board_key = %s AND s.job_ref = v.job_ref "
                  "AND s.is_suppressed = TRUE) ")
        params.append(key)
    query += "ORDER BY v.critical_at, v.job_ref"
    with db_connect() as conn:
        rows = json_rows(conn.execute(query, params).fetchall())
    return jsonify(ok=True, now=now.isoformat(), board=key,
                   future_minutes=config['future_minutes'],
                   past_minutes=config['past_minutes'], jobs=rows)

@app.get('/api/job-board/<job_ref>')
def job_detail(job_ref):
    with db_connect() as conn:
        job = conn.execute('''SELECT job_ref, account_ref, account_name,
                 driver_callsign, driver_firstname, driver_lastname, agent_code, agent_name,
                 vehicle_code, vehicle_description, booked_at, first_seen_at,
                 goods, special_instructions, despatch_instructions, freedom_status,
                 cancelled_at FROM public.freedom_jobs WHERE job_ref=%s''', (job_ref,)).fetchone()
        if job is None: return jsonify(ok=False,error='Job not found'),404
        permitted = conn.execute('''SELECT 1 FROM public.v_timeline_prebooked WHERE job_ref=%s
                   UNION ALL SELECT 1 FROM public.v_timeline_delivery WHERE job_ref=%s
                   UNION ALL SELECT 1 FROM public.v_timeline_callback WHERE job_ref=%s''', (job_ref,job_ref,job_ref)).fetchone()
        if not permitted: return jsonify(ok=False,error='Job is not on this timeline'),404
        stops = json_rows(conn.execute('''SELECT stop_id,drop_order,drop_type,address_name,
             address_line_1,address_line_2,postcode,country_code,required_from,required_to,
             arrived_at,date_completed,deadline_at FROM public.freedom_stops
             WHERE job_ref=%s ORDER BY drop_order,stop_id''',(job_ref,)).fetchall())
        notes = json_rows(conn.execute('''SELECT id,source,sender,subject,note_text,received_at,created_at
             FROM public.job_notes WHERE job_ref=%s
             ORDER BY COALESCE(received_at,created_at),id''',(job_ref,)).fetchall())
        callbacks = conn.execute('''SELECT callback_count,last_callback_at,critical_at AS next_due_at,
                          first_override_minutes, is_priority, required_from, booked_at
                          FROM public.v_timeline_callback WHERE job_ref=%s''',(job_ref,)).fetchone()
        state = conn.execute('''SELECT first_override_minutes,is_suppressed,suppression_reason
                         FROM public.job_callback_state WHERE job_ref=%s''',(job_ref,)).fetchone()
        timeline_suppressions = json_rows(conn.execute('''SELECT board_key,is_suppressed,suppression_reason,updated_at
                         FROM public.timeline_job_suppressions
                         WHERE job_ref=%s AND board_key IN ('prebooked','delivery')''', (job_ref,)).fetchall())
        events = json_rows(conn.execute('''SELECT action_label,message_time
                     FROM public.job_callback_events WHERE job_ref=%s
                     ORDER BY message_time DESC LIMIT 10''',(job_ref,)).fetchall())
        media = json_rows(conn.execute('''SELECT id,source,caption,original_filename,content_type,
             received_at,created_at FROM public.job_media WHERE job_ref=%s
             ORDER BY COALESCE(received_at,created_at),id''',(job_ref,)).fetchall())
    for m in media: m['url'] = url_for('media_download', media_id=m['id'])
    return jsonify(ok=True,job=dict(job),stops=stops,notes=notes,media=media,callbacks=dict(callbacks) if callbacks else None,callback_state=dict(state) if state else None,callback_events=events,
                   timeline_suppressions={r['board_key']:r for r in timeline_suppressions},
                   timeline_removal_reasons=TIMELINE_REMOVAL_REASONS)

@app.get('/job-media/<int:media_id>')
def media_download(media_id):
    with db_connect() as conn:
        row = conn.execute('''SELECT job_ref,storage_bucket,storage_object,content_type,
                                    original_filename FROM public.job_media WHERE id=%s''',
                           (media_id,)).fetchone()
        if row is None: abort(404)
        # Prevent direct media URL access to unrelated/non-qualifying jobs.
        allowed = conn.execute('''''SELECT 1 FROM public.v_timeline_prebooked WHERE job_ref=%s
                   UNION ALL SELECT 1 FROM public.v_timeline_delivery WHERE job_ref=%s
                   UNION ALL SELECT 1 FROM public.v_timeline_callback WHERE job_ref=%s''''',
                               (row['job_ref'],row['job_ref'],row['job_ref'])).fetchone()
        if not allowed: abort(403)
    blob = storage.Client().bucket(row['storage_bucket']).blob(row['storage_object'])
    data = blob.download_as_bytes()
    filename = (row['original_filename'] or 'attachment').replace('"','').replace('\n','')
    return Response(data, mimetype=row['content_type'] or 'application/octet-stream',
                    headers={'Content-Disposition':f'inline; filename="{filename}"',
                             'Cache-Control':'no-store'})


CALLBACK_ACTIONS = {'First Callback', 'Second Callback'}

# Independent, server-validated removal reasons for the other timeline boards.
TIMELINE_REMOVAL_REASONS = {
    'prebooked': ('Internal Booking', 'Other Operational Reason', 'Manual Override'),
    'delivery': ('Manual Override', 'Deadline Not Guaranteed', 'Other Operational Reason'),
}



def _callback_ref(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,39}', value):
        raise ValueError('Invalid job reference')
    return value


def _check_ingest_token():
    expected = os.environ.get('INGEST_TOKEN')
    provided = request.headers.get('X-Ingest-Token', '')
    if not expected or not hmac.compare_digest(expected, provided):
        abort(401)


def _same_origin():
    # Protect browser cookie-authenticated writes from cross-site form requests.
    origin = request.headers.get('Origin', '')
    parsed = urlparse(origin)
    # Compare the public host, not Flask's internally observed scheme: Cloud Run
    # terminates HTTPS before forwarding the request to Flask over HTTP.
    if parsed.scheme != 'https' or parsed.netloc != request.host or parsed.path not in ('', '/'):
        abort(403)


@app.post('/api/ingest/callback')
def ingest_callback():
    # IMPORTANT: board_auth must exempt THIS endpoint from browser Firebase auth,
    # while keeping its X-Ingest-Token check below. See README before deployment.
    _check_ingest_token()
    if not request.is_json: return jsonify(ok=False,error='JSON required'),415
    body = request.get_json(silent=True)
    if not isinstance(body,dict): return jsonify(ok=False,error='Invalid JSON'),400
    try:
        job_ref = _callback_ref(body.get('job_no'))
        action = str(body.get('action') or '').strip()
        if action not in CALLBACK_ACTIONS: raise ValueError('Invalid callback action')
        message_id = str(body.get('message_id') or '').strip()
        if not 1 <= len(message_id) <= 512: raise ValueError('message_id required')
        raw_ts = str(body.get('message_time') or '').strip()
        event_at = datetime.fromisoformat(raw_ts.replace('Z','+00:00'))
        if event_at.tzinfo is None: raise ValueError('message_time must have timezone')
        if event_at > datetime.now(timezone.utc)+timedelta(minutes=5):
            raise ValueError('message_time is in the future')
    except (ValueError,TypeError) as ex:
        return jsonify(ok=False,error=str(ex)),400
    with db_connect() as conn:
        exists = conn.execute('SELECT 1 FROM public.freedom_jobs WHERE job_ref=%s',(job_ref,)).fetchone()
        if not exists: return jsonify(ok=False,error='Unknown job'),404
        inserted = conn.execute('''INSERT INTO public.job_callback_events
           (message_id,job_ref,action_label,message_time) VALUES (%s,%s,%s,%s)
           ON CONFLICT (message_id) DO NOTHING RETURNING message_id''',
           (message_id,job_ref,action,event_at)).fetchone()
        stats = conn.execute('''SELECT count(*)::integer AS callback_count,
                 max(message_time) AS last_callback_at
                 FROM public.job_callback_events WHERE job_ref=%s''',(job_ref,)).fetchone()
        current = conn.execute('''SELECT critical_at FROM public.v_timeline_callback
                         WHERE job_ref=%s''',(job_ref,)).fetchone()
    return jsonify(ok=True,status='accepted' if inserted else 'duplicate',job_no=job_ref,
                   callback_count=stats['callback_count'],last_callback_at=stats['last_callback_at'],
                   next_due_at=current['critical_at'] if current else None)


@app.post('/api/callback/<job_ref>/settings')
def callback_settings(job_ref):
    _same_origin()
    job_ref=_callback_ref(job_ref)
    if not request.is_json: return jsonify(ok=False,error='JSON required'),415
    body=request.get_json(silent=True)
    if not isinstance(body,dict): return jsonify(ok=False,error='Invalid JSON'),400
    allowed={'first_override_minutes','is_suppressed','suppression_reason'}
    if not body or set(body)-allowed: return jsonify(ok=False,error='Invalid settings'),400
    override=body.get('first_override_minutes')
    if override is not None and (type(override) is not int or not 1<=override<=1440):
        return jsonify(ok=False,error='Override must be 1-1440 minutes'),400
    suppressed=body.get('is_suppressed',False)
    if type(suppressed) is not bool: return jsonify(ok=False,error='Invalid suppression flag'),400
    reason=str(body.get('suppression_reason') or '').strip()[:200] if suppressed else None
    if suppressed and not reason: return jsonify(ok=False,error='Suppression reason required'),400
    with db_connect() as conn:
        valid=conn.execute('''SELECT 1 FROM public.freedom_jobs j
           JOIN public.timeline_callback_vehicle_codes codes ON codes.vehicle_code=j.vehicle_code
           WHERE j.job_ref=%s''',(job_ref,)).fetchone()
        if not valid: return jsonify(ok=False,error='Job not eligible'),404
        # Lock the state row; NULL override explicitly clears the override.
        conn.execute('''INSERT INTO public.job_callback_state
            (job_ref,first_override_minutes,is_suppressed,suppression_reason)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (job_ref) DO UPDATE SET
              first_override_minutes=EXCLUDED.first_override_minutes,
              is_suppressed=EXCLUDED.is_suppressed,
              suppression_reason=EXCLUDED.suppression_reason,
              updated_at=now()''',(job_ref,override,suppressed,reason))
    return jsonify(ok=True,job_no=job_ref)

@app.post('/api/timeline/<key>/<job_ref>/suppression')
def timeline_suppression_settings(key, job_ref):
    # Remove or reinstate a job on this timeline only.
    _same_origin()
    reasons = TIMELINE_REMOVAL_REASONS.get(key)
    if reasons is None:
        abort(404)
    try:
        job_ref = _callback_ref(job_ref)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if not request.is_json:
        return jsonify(ok=False, error='JSON required'), 415
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {'is_suppressed', 'suppression_reason'}:
        return jsonify(ok=False, error='Expected is_suppressed and suppression_reason'), 400
    suppressed = body['is_suppressed']
    if type(suppressed) is not bool:
        return jsonify(ok=False, error='Invalid suppression flag'), 400
    reason = body['suppression_reason']
    if suppressed:
        if not isinstance(reason, str) or reason not in reasons:
            return jsonify(ok=False, error='Choose an allowed removal reason'), 400
    elif reason is not None:
        return jsonify(ok=False, error='Reason must be null when reinstating a job'), 400

    # Board view is selected solely from the server-side whitelist.
    with db_connect() as conn:
        eligible = conn.execute(
            f"SELECT 1 FROM {TIMELINES[key]['view']} WHERE job_ref = %s LIMIT 1",
            (job_ref,),
        ).fetchone()
        if not eligible:
            return jsonify(ok=False, error='Job not eligible for this timeline'), 404
        conn.execute('''INSERT INTO public.timeline_job_suppressions
              (board_key, job_ref, is_suppressed, suppression_reason)
              VALUES (%s, %s, %s, %s)
              ON CONFLICT (board_key, job_ref) DO UPDATE SET
                is_suppressed = EXCLUDED.is_suppressed,
                suppression_reason = EXCLUDED.suppression_reason,
                updated_at = now()''', (key, job_ref, suppressed, reason))
    return jsonify(ok=True, board=key, job_no=job_ref,
                   is_suppressed=suppressed, suppression_reason=reason)


@app.get('/health')
def health():
    # board_auth protects this route as well.
    return jsonify(ok=True)
