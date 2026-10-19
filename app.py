"""Read-only Operations Timeline. Freedom is authoritative; no ingest or write routes."""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import psycopg
from psycopg.rows import dict_row
from flask import Flask, Response, abort, jsonify, redirect, render_template, url_for
from google.cloud import storage
from board_auth import init_board_auth

app = Flask(__name__)
init_board_auth(app)

# Whitelisted timeline definitions: never interpolate URL-supplied SQL.
TIMELINES = {
    'prebooked': dict(title='Prebooked Pickups', view='public.v_timeline_prebooked',
                      future_minutes=240, past_minutes=35, stale_hours=24),
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
    query = (f"SELECT * FROM {config['view']} "
             "WHERE critical_at >= %s AND critical_at <= %s ORDER BY critical_at, job_ref")
    with db_connect() as conn:
        rows = json_rows(conn.execute(query, (earliest, latest)).fetchall())
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
        permitted = conn.execute('SELECT 1 FROM public.v_timeline_prebooked WHERE job_ref=%s', (job_ref,)).fetchone()
        if not permitted: return jsonify(ok=False,error='Job is not on this timeline'),404
        stops = json_rows(conn.execute('''SELECT stop_id,drop_order,drop_type,address_name,
             address_line_1,address_line_2,postcode,country_code,required_from,required_to,
             arrived_at,date_completed FROM public.freedom_stops
             WHERE job_ref=%s ORDER BY drop_order,stop_id''',(job_ref,)).fetchall())
        notes = json_rows(conn.execute('''SELECT id,source,sender,subject,note_text,received_at,created_at
             FROM public.job_notes WHERE job_ref=%s
             ORDER BY COALESCE(received_at,created_at),id''',(job_ref,)).fetchall())
        media = json_rows(conn.execute('''SELECT id,source,caption,original_filename,content_type,
             received_at,created_at FROM public.job_media WHERE job_ref=%s
             ORDER BY COALESCE(received_at,created_at),id''',(job_ref,)).fetchall())
    for m in media: m['url'] = url_for('media_download', media_id=m['id'])
    return jsonify(ok=True,job=dict(job),stops=stops,notes=notes,media=media)

@app.get('/job-media/<int:media_id>')
def media_download(media_id):
    with db_connect() as conn:
        row = conn.execute('''SELECT job_ref,storage_bucket,storage_object,content_type,
                                    original_filename FROM public.job_media WHERE id=%s''',
                           (media_id,)).fetchone()
        if row is None: abort(404)
        # Prevent direct media URL access to unrelated/non-qualifying jobs.
        allowed = conn.execute('''SELECT 1 FROM public.v_timeline_prebooked WHERE job_ref=%s''',
                               (row['job_ref'],)).fetchone()
        if not allowed: abort(403)
    blob = storage.Client().bucket(row['storage_bucket']).blob(row['storage_object'])
    data = blob.download_as_bytes()
    filename = (row['original_filename'] or 'attachment').replace('"','').replace('\n','')
    return Response(data, mimetype=row['content_type'] or 'application/octet-stream',
                    headers={'Content-Disposition':f'inline; filename="{filename}"',
                             'Cache-Control':'no-store'})

@app.get('/health')
def health():
    # board_auth protects this route as well.
    return jsonify(ok=True)
