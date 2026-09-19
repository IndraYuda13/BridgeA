# pyrefly: ignore [missing-import]
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from datetime import date, datetime
# pyrefly: ignore [missing-import]
import firebase_admin
# pyrefly: ignore [missing-import]
from firebase_admin import credentials, db as rtdb, storage, firestore as firebase_firestore
# pyrefly: ignore [missing-import]
from google.cloud import firestore
# pyrefly: ignore [missing-import]
from google.cloud.firestore_v1.base_query import FieldFilter
import os
import uuid
# pyrefly: ignore [missing-import]
from werkzeug.utils import secure_filename
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from google.cloud import translate_v2 as translate
# pyrefly: ignore [missing-import]
import google.generativeai as genai
from openai import OpenAI
import json
import re
import base64
import random
import requests
import time
import threading

load_dotenv()

aplikasi = Flask(__name__)
aplikasi.secret_key = os.getenv('SECRET_KEY', 'kunci_rahasia_bridgea_default')
aplikasi.config['SESSION_COOKIE_NAME'] = '__session'

# Konfigurasi Upload Folder
UPLOAD_FOLDER = os.path.join('static', 'uploads', 'modul')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
aplikasi.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Konfigurasi Upload Foto Profil
UPLOAD_PROFIL_FOLDER = os.path.join('static', 'uploads', 'profil')
os.makedirs(UPLOAD_PROFIL_FOLDER, exist_ok=True)
aplikasi.config['UPLOAD_PROFIL_FOLDER'] = UPLOAD_PROFIL_FOLDER

# Set kredensial untuk Google Cloud Translation
credentials_path = 'firebase-credentials.json'
if os.path.exists(credentials_path):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = credentials_path

try:
    translate_client = translate.Client()
except Exception as e:
    print(f"Peringatan: Gagal menginisialisasi Google Cloud Translation: {e}")
    translate_client = None

# Inisialisasi Firebase
firebase_database_url = os.getenv('FIREBASE_DATABASE_URL', 'https://bridgesign-default-rtdb.firebaseio.com/')
firebase_storage_bucket = os.getenv('FIREBASE_STORAGE_BUCKET', 'bridgesign.firebasestorage.app')

if os.path.exists(credentials_path):
    cred = credentials.Certificate(credentials_path)
    firebase_admin.initialize_app(cred, {
        'databaseURL': firebase_database_url,
        'storageBucket': firebase_storage_bucket
    })
else:
    # Menggunakan Application Default Credentials (ADC) di server Google Cloud/Firebase
    firebase_admin.initialize_app(options={
        'databaseURL': firebase_database_url,
        'storageBucket': firebase_storage_bucket
    })
db = firebase_firestore.client()

# =====================================================
# SIMPLE IN-MEMORY CACHE untuk data Firestore
# Mengurangi query berulang ke database setiap request
# =====================================================
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 menit dalam detik

def get_cache(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry['time']) < CACHE_TTL:
            return entry['data']
        return None

def set_cache(key, data):
    with _cache_lock:
        _cache[key] = {'data': data, 'time': time.time()}

def clear_cache(key=None):
    with _cache_lock:
        if key:
            _cache.pop(key, None)
        else:
            _cache.clear()


def format_phone_number(phone):
    # Bersihkan karakter non-digit
    clean_phone = re.sub(r'\D', '', phone)
    if clean_phone.startswith('0'):
        clean_phone = '62' + clean_phone[1:]
    elif clean_phone.startswith('8'):
        clean_phone = '62' + clean_phone
    return clean_phone

def kirim_wa_fonnte(target, message):
    token = os.getenv('FONNTE_API_TOKEN')
    if not token:
        print("Peringatan: FONNTE_API_TOKEN tidak diatur di file .env.")
        return False, "TOKEN_NOT_CONFIGURED"
        
    url = "https://api.fonnte.com/send"
    headers = {
        "Authorization": token
    }
    payload = {
        "target": format_phone_number(target),
        "message": message
    }
    try:
        response = requests.post(url, headers=headers, data=payload)
        response_json = response.json()
        print(f"Fonnte Response: {response_json}")
        status = response_json.get('status', False)
        reason = response_json.get('reason') or response_json.get('detail') or 'Unknown error'
        return status, reason
    except Exception as e:
        print(f"Error mengirim pesan Fonnte: {e}")
        return False, str(e)

# Inisialisasi Gemini jika API key tersedia
gemini_key = os.getenv('GEMINI_API_KEY')
if gemini_key:
    genai.configure(api_key=gemini_key)

# ==========================================
# UNIFIED AI CLIENT (OpenAI & Gemini Fallback)
# ==========================================

class UnifiedAIResponse:
    """Wrapper response seragam dengan atribut .text dan representasi string."""
    def __init__(self, text=""):
        self.text = text or ""

    def __str__(self):
        return self.text


class UnifiedAIClient:
    """
    Arsitektur AI Terpadu & Tangguh (Multi-tier Fallback):
    1. Primary: Library resmi OpenAI SDK (mendukung custom OPENAI_BASE_URL & OPENAI_MODEL).
    2. Fallback: Google Gemini SDK jika GEMINI_API_KEY terkonfigurasi.
    3. Failover: Exception dilempar agar endpoint mengeksekusi fungsi fallback manual bawaan.
    """
    def __init__(self, model=None):
        self.openai_base_url = os.getenv('OPENAI_BASE_URL') or None
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        env_openai_model = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
        # Gunakan model kustom jika disediakan dan bukan string legacy gemini, selain itu pakai OPENAI_MODEL
        self.openai_model = env_openai_model if (model is None or 'gemini' in str(model).lower()) else model
        self.gemini_model = model if (model and 'gemini' in str(model).lower()) else os.getenv('GEMINI_MODEL', 'gemini-1.5-flash')
        
        try:
            self.timeout = int(os.getenv('AI_TIMEOUT', '30'))
        except (ValueError, TypeError):
            self.timeout = 30

    def generate_content(self, prompt, **kwargs):
        """Metode serbaguna kompatibel dengan Gemini SDK & OpenAI."""
        return self.generate(prompt, **kwargs)

    def generate(self, prompt, **kwargs):
        """Mengeksekusi generasi teks dengan proteksi failover berlapis."""
        prompt_str = prompt if isinstance(prompt, str) else str(prompt)
        last_error = None

        # Tier 1: Official OpenAI SDK
        if self.openai_api_key:
            try:
                client = OpenAI(
                    api_key=self.openai_api_key,
                    base_url=self.openai_base_url if self.openai_base_url else None,
                    timeout=self.timeout
                )
                stream = kwargs.get('stream', False)
                response = client.chat.completions.create(
                    model=self.openai_model,
                    messages=[
                        {"role": "user", "content": prompt_str}
                    ],
                    stream=stream,
                    timeout=self.timeout
                )
                
                if stream:
                    chunks = []
                    for chunk in response:
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            chunks.append(chunk.choices[0].delta.content)
                    content = "".join(chunks)
                else:
                    content = ""
                    if response.choices and response.choices[0].message and response.choices[0].message.content:
                        content = response.choices[0].message.content

                if content:
                    return UnifiedAIResponse(content)
                raise RuntimeError("OpenAI mengembalikan respons kosong.")
            except Exception as e_openai:
                print(f"[UnifiedAIClient] OpenAI call gagal ({e_openai}), mengalihkan ke fallback...")
                last_error = e_openai

        # Tier 2: Google Gemini SDK Fallback
        gemini_key = os.getenv('GEMINI_API_KEY')
        if gemini_key:
            try:
                genai.configure(api_key=gemini_key)
                model = genai.GenerativeModel(self.gemini_model)
                req_opts = {"timeout": self.timeout}
                if 'request_options' in kwargs:
                    req_opts.update(kwargs.pop('request_options'))
                resp = model.generate_content(prompt_str, request_options=req_opts, **kwargs)
                if resp and hasattr(resp, 'text') and resp.text:
                    return UnifiedAIResponse(resp.text)
                raise RuntimeError("Gemini mengembalikan respons kosong.")
            except Exception as e_gemini:
                print(f"[UnifiedAIClient] Gemini fallback gagal ({e_gemini}).")
                last_error = e_gemini

        # Tier 3: Jika seluruh AI gagal / API key tidak ada, lemparkan error ke endpoint
        error_msg = (
            f"Semua penyedia AI gagal. Error terakhir: {last_error}"
            if last_error
            else "Tidak ada API key AI yang terkonfigurasi (OPENAI_API_KEY / GEMINI_API_KEY)."
        )
        raise RuntimeError(error_msg)


# Alias untuk backward compatibility
SafeAIClient = UnifiedAIClient
SafeGenerativeModel = UnifiedAIClient
SafeAIResponse = UnifiedAIResponse

@aplikasi.before_request
def batasi_akses():
    rute = request.path
    
    # Batasi akses rute admin
    if rute.startswith('/admin'):
        if session.get('role') != 'admin':
            return redirect(url_for('login_admin'))
            
    # Batasi akses rute kepala sekolah
    elif rute.startswith('/kepsek'):
        if session.get('role') != 'kepala-sekolah':
            return redirect(url_for('login_kepsek'))
            
    # Batasi akses rute guru
    elif rute.startswith('/guru'):
        if session.get('role') != 'guru':
            return redirect(url_for('login_guru'))

@aplikasi.route('/')
def halaman_utama():
    return render_template('index.html')

@aplikasi.route('/login/admin', methods=['GET', 'POST'])
def login_admin():
    error = False
    if request.method == 'POST':
        nip = request.form.get('nip')
        password = request.form.get('password')
        
        admin_ref = db.collection('admin').where(filter=FieldFilter('nip', '==', nip)).where(filter=FieldFilter('password', '==', password)).stream()
        admin_ditemukan = False
        admin_data = None
        for doc in admin_ref:
            admin_ditemukan = True
            admin_data = doc.to_dict()
            admin_data['id'] = doc.id
            break
            
        if admin_ditemukan:
            session['logged_in_user'] = admin_data
            session['role'] = 'admin'
            return redirect(url_for('dashboard_admin', login='success'))
        else:
            error = True
            
    return render_template('login_admin.html', error=error)

@aplikasi.route('/login/kepala-sekolah', methods=['GET', 'POST'])
def login_kepsek():
    error = False
    if request.method == 'POST':
        nip = request.form.get('nip')
        password = request.form.get('password')
        
        kepsek_ref = db.collection('kepsek').where(filter=FieldFilter('nip', '==', nip)).where(filter=FieldFilter('password', '==', password)).stream()
        kepsek_ditemukan = False
        kepsek_data = None
        for doc in kepsek_ref:
            kepsek_ditemukan = True
            kepsek_data = doc.to_dict()
            kepsek_data['id'] = doc.id
            break
            
        if kepsek_ditemukan:
            session['logged_in_user'] = kepsek_data
            session['role'] = 'kepala-sekolah'
            return redirect(url_for('dashboard_kepsek', login='success'))
        else:
            error = True
            
    return render_template('login_kepsek.html', error=error)

@aplikasi.route('/login/guru', methods=['GET', 'POST'])
def login_guru():
    error = False
    if request.method == 'POST':
        nip = request.form.get('nip')
        password = request.form.get('password')
        
        gurus_ref = db.collection('guru').where(filter=FieldFilter('nip', '==', nip)).where(filter=FieldFilter('password', '==', password)).stream()
        guru_ditemukan = False
        guru_data = None
        for doc in gurus_ref:
            guru_ditemukan = True
            guru_data = doc.to_dict()
            guru_data['id'] = doc.id
            break
            
        if guru_ditemukan:
            session['logged_in_user'] = guru_data
            session['role'] = 'guru'
            return redirect(url_for('dashboard_guru', login='success'))
        else:
            error = True
            
    return render_template('login_guru.html', error=error)

@aplikasi.route('/lupa-kata-sandi', methods=['GET', 'POST'])
def lupa_kata_sandi():
    if request.method == 'POST':
        no_wa = request.form.get('no_wa', '').strip()
        password = request.form.get('password', '').strip()
        nip = request.form.get('nip', '').strip()
        
        # Validasi apakah OTP sudah sukses diverifikasi lewat API sebelumnya
        if not session.get('otp_verified') or session.get('otp_nip') != nip or not no_wa.endswith(session.get('otp_wa', '')[-8:]):
            return render_template('lupa_kata_sandi.html', error="Verifikasi OTP gagal atau sesi telah kedaluwarsa.")
            
        # Hapus session OTP setelah berhasil digunakan
        session.pop('otp_code', None)
        session.pop('otp_wa', None)
        session.pop('otp_nip', None)
        session_role = session.pop('otp_role', None)
        session_doc_id = session.pop('otp_doc_id', None)
        session.pop('otp_verified', None)
        
        try:
            # Update password dan daftarkan nomor WA jika belum terdaftar
            db.collection(session_role).document(session_doc_id).update({
                'password': password,
                'no_wa': no_wa
            })
            
            # Alihkan ke halaman login yang sesuai dengan peran
            if session_role == 'admin':
                return redirect(url_for('login_admin', reset_success='true'))
            elif session_role == 'kepsek':
                return redirect(url_for('login_kepsek', reset_success='true'))
            else:
                return redirect(url_for('login_guru', reset_success='true'))
        except Exception as e:
            return render_template('lupa_kata_sandi.html', error=f"Gagal memperbarui kata sandi: {e}")
            
    return render_template('lupa_kata_sandi.html')

@aplikasi.route('/api/verifikasi_otp', methods=['POST'])
def verifikasi_otp():
    data_masuk = request.json
    otp_input = str(data_masuk.get('otp', '')).strip()
    no_wa = str(data_masuk.get('no_wa', '')).strip()
    nip = str(data_masuk.get('nip', '')).strip()
    
    session_otp = session.get('otp_code')
    session_wa = session.get('otp_wa')
    session_nip = session.get('otp_nip')
    
    if (session_otp and session_wa and session_nip and 
        session_otp == otp_input and session_nip == nip and no_wa.endswith(session_wa[-8:])):
        # Tandai di session bahwa OTP sudah berhasil diverifikasi
        session['otp_verified'] = True
        return jsonify({'valid': True})
        
    return jsonify({'valid': False, 'message': 'Kode OTP yang dimasukkan salah.'})

@aplikasi.route('/api/cek_wa', methods=['POST'])
def cek_wa():
    data_masuk = request.json
    no_wa = str(data_masuk.get('no_wa', '')).strip()
    nip = str(data_masuk.get('nip', '')).strip()
    
    if no_wa and nip:
        target_role = None
        target_doc_id = None
        
        # Cari di guru, kepsek, admin
        for role in ['guru', 'kepsek', 'admin']:
            for doc in db.collection(role).stream():
                data_user = doc.to_dict()
                nip_db = str(data_user.get('nip', '')).strip()
                wa_db = str(data_user.get('no_wa', '')).strip()
                
                # Cek apakah NIP cocok
                if nip_db == nip:
                    # Jika no_wa sudah ada di DB, harus cocok (8 digit terakhir)
                    if wa_db:
                        if len(wa_db) >= 8 and len(no_wa) >= 8 and no_wa.endswith(wa_db[-8:]):
                            target_role = role
                            target_doc_id = doc.id
                            break
                    else:
                        # Fallback jika admin/kepsek belum punya nomor WA terdaftar, 
                        # izinkan nomor yang diinput dan daftarkan otomatis nanti
                        target_role = role
                        target_doc_id = doc.id
                        break
            if target_role:
                break
                
        if target_role and target_doc_id:
            # Cek rate limiting: cegah kirim OTP ke nomor yang sama dalam 2 menit
            rate_key = f"otp_sent_{format_phone_number(no_wa)}"
            last_sent = get_cache(rate_key)
            if last_sent:
                sisa_detik = int(30 - (time.time() - last_sent))
                if sisa_detik > 0:
                    return jsonify({
                        'exists': True, 
                        'sent': False, 
                        'message': f'Tunggu {sisa_detik} detik sebelum kirim OTP lagi.'
                    })

            # 1. Generate OTP
            otp = str(random.randint(1000, 9999))

            
            # 2. Simpan ke session
            session['otp_code'] = otp
            session['otp_wa'] = no_wa
            session['otp_nip'] = nip
            session['otp_role'] = target_role
            session['otp_doc_id'] = target_doc_id
            
            # 3. Kirim via Fonnte - gunakan pesan natural agar tidak terdeteksi spam
            templates_pesan = [
                f"Halo! Kode verifikasi BridgeA kamu adalah *{otp}*. Berlaku 5 menit ya 🙏",
                f"Hai, ini kode OTP BridgeA kamu: *{otp}*. Jangan kasih ke siapapun ya!",
                f"Kode masuk BridgeA: *{otp}* — Abaikan jika bukan kamu yang minta.",
                f"BridgeA - Kode verifikasi: *{otp}*. Kode berlaku selama 5 menit.",
            ]
            pesan = random.choice(templates_pesan)
            status_kirim, detail_kirim = kirim_wa_fonnte(no_wa, pesan)
            
            if status_kirim:
                # Catat waktu pengiriman untuk rate limiting (2 menit)
                set_cache(rate_key, time.time())
                return jsonify({'exists': True, 'sent': True})
            else:
                return jsonify({'exists': True, 'sent': False, 'message': f'Gagal mengirim OTP: {detail_kirim}'})
                
    return jsonify({'exists': False})




def ambil_data_dashboard():
    """Helper: Ambil data guru & kelas dengan caching 5 menit."""
    gurus = get_cache('guru_list')
    if gurus is None:
        gurus = [doc.to_dict() for doc in db.collection('guru').stream()]
        set_cache('guru_list', gurus)

    kelas = get_cache('kelas_list')
    if kelas is None:
        kelas = [doc.to_dict() for doc in db.collection('kelas').stream()]
        set_cache('kelas_list', kelas)

    # Buat index guru by nama untuk O(1) lookup
    guru_index = {str(g.get('nama', '')).lower(): g.get('nip', '-') for g in gurus}

    pemantauan_data = []
    for k in kelas:
        guru_nama = k.get('guru_pengajar', '')
        pemantauan_data.append({
            'nama_guru': guru_nama,
            'nip': guru_index.get(guru_nama.lower(), '-'),
            'nama_kelas': k.get('nama_kelas', ''),
            'jadwal_hari': k.get('jadwal_hari', ''),
            'jadwal_waktu_mulai': k.get('jadwal_waktu_mulai', ''),
            'jadwal_waktu_selesai': k.get('jadwal_waktu_selesai', '')
        })
    return gurus, kelas, pemantauan_data

@aplikasi.route('/admin/dashboard')
def dashboard_admin():
    gurus, kelas, pemantauan_data = ambil_data_dashboard()
    return render_template('admin_dashboard.html',
                           total_guru=len(gurus),
                           total_kelas=len(kelas),
                           pemantauan_data=pemantauan_data)

@aplikasi.route('/admin/guru', methods=['GET', 'POST'])
def kelola_guru():
    if request.method == 'POST':
        nama_guru = request.form.get('nama_guru')
        nip = request.form.get('nip')
        no_wa = request.form.get('no_wa', '')
        password = request.form.get('password', '')
        if nama_guru and nip:
            db.collection('guru').add({
                'nama': nama_guru, 
                'nip': nip,
                'no_wa': no_wa,
                'password': password
            })
            clear_cache('guru_list')  # invalidate cache
        return redirect(url_for('kelola_guru'))
    
    q = str(request.args.get('q', '')).lower()
    filtered_guru = []
    for doc in db.collection('guru').stream():
        g = doc.to_dict()
        g['id'] = doc.id
        if q:
            if q in str(g.get('nama', '')).lower() or q in str(g.get('nip', '')).lower():
                filtered_guru.append(g)
        else:
            filtered_guru.append(g)
        
    return render_template('admin_guru.html', daftar_guru=filtered_guru)

@aplikasi.route('/admin/guru/edit/<string:id>', methods=['POST'])
def edit_guru(id):
    nama_guru = request.form.get('nama_guru')
    nip = request.form.get('nip')
    no_wa = request.form.get('no_wa', '')
    password = request.form.get('password', '')
    if nama_guru and nip:
        db.collection('guru').document(id).update({
            'nama': nama_guru, 
            'nip': nip,
            'no_wa': no_wa,
            'password': password
        })
        clear_cache('guru_list')  # invalidate cache
    return redirect(url_for('kelola_guru'))

@aplikasi.route('/admin/guru/hapus/<string:id>', methods=['POST'])
def hapus_guru(id):
    db.collection('guru').document(id).delete()
    clear_cache('guru_list')  # invalidate cache
    return redirect(url_for('kelola_guru'))

@aplikasi.route('/admin/kelas', methods=['GET', 'POST'])
def kelola_kelas():
    if request.method == 'POST':
        nama_kelas = request.form.get('nama_kelas')
        guru_pengajar = request.form.get('guru_pengajar')
        hari = request.form.get('hari')
        waktu_mulai = request.form.get('waktu_mulai')
        waktu_selesai = request.form.get('waktu_selesai')
        
        if nama_kelas and guru_pengajar and hari and waktu_mulai and waktu_selesai:
            db.collection('kelas').add({
                'nama_kelas': nama_kelas,
                'guru_pengajar': guru_pengajar,
                'jadwal_hari': hari,
                'jadwal_waktu_mulai': waktu_mulai,
                'jadwal_waktu_selesai': waktu_selesai
            })
            clear_cache('kelas_list')  # invalidate cache
        return redirect(url_for('kelola_kelas'))
    
    q = str(request.args.get('q', '')).lower()
    filtered_kelas = []
    for doc in db.collection('kelas').stream():
        k = doc.to_dict()
        k['id'] = doc.id
        if q:
            if q in str(k.get('nama_kelas', '')).lower() or q in str(k.get('guru_pengajar', '')).lower():
                filtered_kelas.append(k)
        else:
            filtered_kelas.append(k)
            
    return render_template('admin_kelas.html', daftar_kelas=filtered_kelas)

@aplikasi.route('/admin/kelas/edit/<string:id>', methods=['POST'])
def edit_kelas(id):
    nama_kelas = request.form.get('nama_kelas')
    guru_pengajar = request.form.get('guru_pengajar')
    hari = request.form.get('hari')
    waktu_mulai = request.form.get('waktu_mulai')
    waktu_selesai = request.form.get('waktu_selesai')
    
    if nama_kelas and guru_pengajar and hari and waktu_mulai and waktu_selesai:
        db.collection('kelas').document(id).update({
            'nama_kelas': nama_kelas,
            'guru_pengajar': guru_pengajar,
            'jadwal_hari': hari,
            'jadwal_waktu_mulai': waktu_mulai,
            'jadwal_waktu_selesai': waktu_selesai
        })
        clear_cache('kelas_list')  # invalidate cache
    return redirect(url_for('kelola_kelas'))

@aplikasi.route('/admin/kelas/hapus/<string:id>', methods=['POST'])
def hapus_kelas(id):
    db.collection('kelas').document(id).delete()
    clear_cache('kelas_list')  # invalidate cache
    return redirect(url_for('kelola_kelas'))

@aplikasi.route('/kepsek/dashboard')
def dashboard_kepsek():
    gurus, kelas, pemantauan_data = ambil_data_dashboard()
    return render_template('kepsek_dashboard.html',
                           total_guru=len(gurus),
                           total_kelas=len(kelas),
                           pemantauan_data=pemantauan_data)

@aplikasi.route('/kepsek/guru')
def kepsek_guru():
    q = str(request.args.get('q', '')).lower()
    filtered_guru = []
    for doc in db.collection('guru').stream():
        g = doc.to_dict()
        if q:
            if q in str(g.get('nama', '')).lower() or q in str(g.get('nip', '')).lower():
                filtered_guru.append(g)
        else:
            filtered_guru.append(g)
    return render_template('kepsek_guru.html', daftar_guru=filtered_guru)

@aplikasi.route('/kepsek/kelas')
def kepsek_kelas():
    q = str(request.args.get('q', '')).lower()
    filtered_kelas = []
    for doc in db.collection('kelas').stream():
        k = doc.to_dict()
        if q:
            if q in str(k.get('nama_kelas', '')).lower() or q in str(k.get('guru_pengajar', '')).lower():
                filtered_kelas.append(k)
        else:
            filtered_kelas.append(k)
    return render_template('kepsek_kelas.html', daftar_kelas=filtered_kelas)

@aplikasi.route('/kepsek/riwayat')
def kepsek_riwayat():
    # Ambil data dari Firebase dan urutkan dari yang terbaru
    semua_dokumen = db.collection('riwayat_sesi').order_by('timestamp', direction=firestore.Query.DESCENDING).stream()
    daftar_riwayat = []
    
    # Kamus buat ganti nama hari dan bulan ke bahasa Indonesia
    hari_indo = {
        'Monday': 'Senin', 'Tuesday': 'Selasa', 'Wednesday': 'Rabu',
        'Thursday': 'Kamis', 'Friday': 'Jumat', 'Saturday': 'Sabtu', 'Sunday': 'Minggu'
    }
    bulan_indo = {
        'January': 'Januari', 'February': 'Februari', 'March': 'Maret',
        'April': 'April', 'May': 'Mei', 'June': 'Juni',
        'July': 'Juli', 'August': 'Agustus', 'September': 'September',
        'October': 'Oktober', 'November': 'November', 'December': 'Desember'
    }
    
    # Ambil data kelas untuk memetakan nama_kelas ke guru_pengajar
    kelas_dict = {}
    for doc in db.collection('kelas').stream():
        k = doc.to_dict()
        if 'nama_kelas' in k:
            kelas_dict[k['nama_kelas']] = k.get('guru_pengajar', 'Tidak Diketahui')

    for doc in semua_dokumen:
        data_sesi = doc.to_dict()
        data_sesi['id'] = doc.id
        
        # Tambahkan nama_guru
        nama_kelas = data_sesi.get('nama_kelas', 'Sesi STT')
        data_sesi['nama_guru'] = kelas_dict.get(nama_kelas, 'Tidak Diketahui')
        
        # Bikin format tanggal yang rapi
        if 'timestamp' in data_sesi and data_sesi['timestamp']:
            waktu = data_sesi['timestamp']
            nama_hari = hari_indo.get(waktu.strftime('%A'), waktu.strftime('%A'))
            nama_bulan = bulan_indo.get(waktu.strftime('%B'), waktu.strftime('%B'))
            
            data_sesi['tanggal_format'] = f"{nama_hari}, {waktu.strftime('%d')} {nama_bulan} {waktu.strftime('%Y')} {waktu.strftime('%H:%M')}"
            data_sesi['tanggal_saja'] = f"{nama_hari}, {waktu.strftime('%d')} {nama_bulan} {waktu.strftime('%Y')}"
            data_sesi['waktu_saja'] = waktu.strftime('%H:%M')
            data_sesi['tanggal_dan_waktu'] = f"{waktu.strftime('%d')} {nama_bulan[:3]} {waktu.strftime('%Y')}, {waktu.strftime('%H:%M')}"
        else:
            data_sesi['tanggal_format'] = 'Waktu tidak tersedia'
            data_sesi['tanggal_saja'] = '-'
            data_sesi['waktu_saja'] = '-'
            data_sesi['tanggal_dan_waktu'] = '-'
            
        # Cuplikan kata bahasa Inggris
        if 'riwayat' in data_sesi and len(data_sesi['riwayat']) > 0:
            kata_fokus_list = []
            semua_teks_inggris = ""
            for item in data_sesi['riwayat']:
                teks_bersih = re.sub('<[^<]+>', '', item.get('en', ''))
                semua_teks_inggris += teks_bersih + " "
                
                # Ambil beberapa kata unik huruf besar dari teks bahasa Inggris
                kata_kata = [k.upper().strip() for k in teks_bersih.split() if k.strip() != '']
                for k in kata_kata:
                    kata_bersih = re.sub('[^A-Z]', '', k)
                    if kata_bersih and len(kata_bersih) > 1 and kata_bersih not in kata_fokus_list:
                        kata_fokus_list.append(kata_bersih)
            
            # Ambil max 3 kata
            data_sesi['topik_fokus'] = ", ".join(kata_fokus_list[:3])
            if len(semua_teks_inggris) > 100:
                data_sesi['preview_en'] = semua_teks_inggris[:100] + '...'
            else:
                data_sesi['preview_en'] = semua_teks_inggris
        else:
            data_sesi['topik_fokus'] = '-'
            data_sesi['preview_en'] = '-'
            
        isi_riwayat = data_sesi.get('riwayat', [])
        teks_json = json.dumps(isi_riwayat)
        data_sesi['riwayat_b64'] = base64.b64encode(teks_json.encode('utf-8')).decode('utf-8')
        
        isi_catatan = data_sesi.get('catatan', '')
        if isi_catatan == '':
            isi_catatan = 'Tidak ada catatan.'
        catatan_json = json.dumps(isi_catatan)
        data_sesi['catatan_b64'] = base64.b64encode(catatan_json.encode('utf-8')).decode('utf-8')
        
        isi_feedback = data_sesi.get('feedback_kepsek', '')
        feedback_json = json.dumps(isi_feedback)
        data_sesi['feedback_b64'] = base64.b64encode(feedback_json.encode('utf-8')).decode('utf-8')
        data_sesi['has_feedback'] = bool(isi_feedback)
            
        daftar_riwayat.append(data_sesi)
        
    return render_template('kepsek_riwayat.html', daftar_riwayat=daftar_riwayat)

@aplikasi.route('/guru/dashboard')
def dashboard_guru():
    logged_in = session.get('logged_in_user')  # type: ignore  # type: ignore
    guru_aktif = logged_in if isinstance(logged_in, dict) else {'nama': 'Guru Belum Ada', 'nip': '-'}
    
    kelas = []
    for doc in db.collection('kelas').stream():
        kelas.append(doc.to_dict())

    # Cari kelas yang diajar sama guru ini aja
    kelas_guru = []
    for k in kelas:
        if str(k.get('guru_pengajar', '')).lower() == str(guru_aktif.get('nama', '')).lower():
            kelas_guru.append(k)
            
    hari_ini = date.today().strftime('%Y-%m-%d')
    
    return render_template('guru_dashboard.html', guru=guru_aktif, kelas_guru=kelas_guru, hari_ini=hari_ini)

@aplikasi.route('/guru/modul', methods=['GET', 'POST'])
def guru_modul():
    logged_in = session.get('logged_in_user')  # type: ignore  # type: ignore
    guru_aktif = logged_in if isinstance(logged_in, dict) else {'nama': 'Guru Belum Ada', 'nip': '-'}
    
    if request.method == 'POST':
        if 'modul_file' not in request.files:
            return redirect(request.url)
        file = request.files['modul_file']
        if file.filename == '':
            return redirect(request.url)
            
        if file:
            filename = secure_filename(file.filename)
            file_ext = os.path.splitext(filename)[1].lower()
            
            allowed_exts = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.jpg', '.png']
            if file_ext not in allowed_exts:
                return redirect(request.url)
                
            file.seek(0, os.SEEK_END)
            file_length = file.tell()
            if file_length > 2 * 1024 * 1024:
                return redirect(request.url)
            file.seek(0)
            
            unique_id = str(uuid.uuid4())[:8]
            safe_filename = f"{unique_id}_{filename}"
            
            # 1. Ambil keranjang Firebase Storage
            bucket = storage.bucket()
            blob = bucket.blob(f'modul_pembelajaran/{safe_filename}')
            
            # 2. Upload langsung dari file (tanpa harus save di komputer lokal)
            file.seek(0)
            blob.upload_from_file(file, content_type=file.content_type)
            
            # 3. Buka akses publik agar bisa didownload
            blob.make_public()
            public_url = blob.public_url
            
            data_modul = {
                'id': unique_id,
                'nama_file': filename,
                'path_file': safe_filename,
                'url_download': public_url,
                'ukuran_byte': file_length,
                'tanggal_upload': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            db.collection('modul_pembelajaran').document(unique_id).set(data_modul)
            return redirect(url_for('guru_modul'))
            
    daftar_modul = []
    modul_ref = db.collection('modul_pembelajaran').stream()
    for doc in modul_ref:
        m = doc.to_dict()
        m['id'] = doc.id
        size_kb = m.get('ukuran_byte', 0) / 1024
        if size_kb > 1024:
            m['ukuran_format'] = f"{size_kb / 1024:.2f} MB"
        else:
            m['ukuran_format'] = f"{size_kb:.0f} KB"
        daftar_modul.append(m)
        
    daftar_modul.sort(key=lambda x: x.get('tanggal_upload', ''), reverse=True)
            
    return render_template('guru_modul.html', guru=guru_aktif, daftar_modul=daftar_modul)

@aplikasi.route('/guru/modul/hapus/<string:id>', methods=['POST'])
def hapus_modul(id):
    doc_ref = db.collection('modul_pembelajaran').document(id)
    doc = doc_ref.get()
    
    if doc.exists:
        data = doc.to_dict()
        path_file = data.get('path_file', '')
        
        # Hapus file dari Firebase Storage
        if path_file:
            try:
                bucket = storage.bucket()
                blob = bucket.blob(f'modul_pembelajaran/{path_file}')
                blob.delete()
            except Exception as e:
                print(f"Gagal menghapus file di Storage: {e}")
                
        # Hapus doc dari Firestore
        doc_ref.delete()
        
    return redirect(url_for('guru_modul'))

@aplikasi.route('/guru/riwayat')
def guru_riwayat():
    logged_in = session.get('logged_in_user')  # type: ignore  # type: ignore
    guru_aktif = logged_in if isinstance(logged_in, dict) else {'nama': 'Guru Belum Ada', 'nip': '-'}
    
    kelas = []
    for doc in db.collection('kelas').stream():
        k_data = doc.to_dict()
        k_data['id'] = doc.id
        kelas.append(k_data)
        
    daftar_folder = []
    for k in kelas:
        if str(k.get('guru_pengajar', '')).lower() == str(guru_aktif.get('nama', '')).lower():
            daftar_folder.append(k)
            
    return render_template('guru_riwayat_folder.html', guru=guru_aktif, daftar_folder=daftar_folder)

@aplikasi.route('/guru/tambah_folder', methods=['POST'])
def guru_tambah_folder():
    nama_folder = request.form.get('nama_folder')
    if not nama_folder:
        return redirect(url_for('guru_riwayat'))
        
    logged_in = session.get('logged_in_user')  # type: ignore  # type: ignore
    guru_aktif = logged_in if isinstance(logged_in, dict) else {'nama': 'Guru Belum Ada'}
        
    # Simpan folder sebagai entitas kelas baru yang terikat dengan guru tersebut
    doc_ref = db.collection('kelas').document()
    doc_ref.set({
        'nama_kelas': nama_folder,
        'guru_pengajar': guru_aktif.get('nama', ''),
        'jadwal_hari': 'TBA',
        'jadwal_waktu_mulai': '00:00',
        'jadwal_waktu_selesai': '00:00'
    })
    
    return redirect(url_for('guru_riwayat'))

@aplikasi.route('/guru/hapus_folder/<id_kelas>', methods=['POST'])
def guru_hapus_folder(id_kelas):
    try:
        db.collection('kelas').document(id_kelas).delete()
    except Exception:
        pass
    return redirect(url_for('guru_riwayat'))

@aplikasi.route('/guru/hapus_riwayat/<id_riwayat>', methods=['POST'])
def guru_hapus_riwayat(id_riwayat):
    nama_kelas = request.args.get('nama_kelas', '')
    try:
        db.collection('riwayat_sesi').document(id_riwayat).delete()
    except Exception:
        pass
    
    if nama_kelas:
        return redirect(url_for('guru_riwayat_kelas', nama_kelas=nama_kelas))
    return redirect(url_for('guru_riwayat'))

@aplikasi.route('/guru/riwayat/<nama_kelas>')
def guru_riwayat_kelas(nama_kelas):
    logged_in = session.get('logged_in_user')  # type: ignore  # type: ignore
    guru_aktif = logged_in if isinstance(logged_in, dict) else {'nama': 'Guru Belum Ada', 'nip': '-'}
    # Ambil data dari Firebase tanpa order_by untuk menghindari error Index Firestore
    semua_dokumen = db.collection('riwayat_sesi').where(filter=FieldFilter('nama_kelas', '==', nama_kelas)).stream()
    daftar_riwayat = []
    
    # Kamus buat ganti nama hari dan bulan ke bahasa Indonesia
    hari_indo = {
        'Monday': 'Senin', 'Tuesday': 'Selasa', 'Wednesday': 'Rabu',
        'Thursday': 'Kamis', 'Friday': 'Jumat', 'Saturday': 'Sabtu', 'Sunday': 'Minggu'
    }
    bulan_indo = {
        'January': 'Januari', 'February': 'Februari', 'March': 'Maret',
        'April': 'April', 'May': 'Mei', 'June': 'Juni',
        'July': 'Juli', 'August': 'Agustus', 'September': 'September',
        'October': 'Oktober', 'November': 'November', 'December': 'Desember'
    }
    
    for doc in semua_dokumen:
        data_sesi = doc.to_dict()
        data_sesi['id'] = doc.id
        
        # Bikin format tanggal yang rapi
        if 'timestamp' in data_sesi and data_sesi['timestamp']:
            waktu = data_sesi['timestamp']
            nama_hari = hari_indo.get(waktu.strftime('%A'), waktu.strftime('%A'))
            nama_bulan = bulan_indo.get(waktu.strftime('%B'), waktu.strftime('%B'))
            
            data_sesi['tanggal_format'] = f"{nama_hari}, {waktu.strftime('%d')} {nama_bulan} {waktu.strftime('%Y')} {waktu.strftime('%H:%M')}"
            data_sesi['tanggal_saja'] = f"{nama_hari}, {waktu.strftime('%d')} {nama_bulan} {waktu.strftime('%Y')}"
            data_sesi['waktu_saja'] = waktu.strftime('%H:%M')
            data_sesi['tanggal_iso'] = waktu.strftime('%Y-%m-%d')
        else:
            data_sesi['tanggal_format'] = 'Waktu tidak tersedia'
            data_sesi['tanggal_saja'] = '-'
            data_sesi['waktu_saja'] = '-'
            data_sesi['tanggal_iso'] = ''
            
        # Potong teks buat ditampilkan sedikit aja (preview) di kartu
        if 'riwayat' in data_sesi and len(data_sesi['riwayat']) > 0:
            semua_teks_indo = ""
            semua_teks_inggris = ""
            
            for item in data_sesi['riwayat']:
                semua_teks_indo += item.get('id', '') + " "
                # Bersihkan tag HTML biar rapi
                teks_bersih = re.sub('<[^<]+>', '', item.get('en', ''))
                semua_teks_inggris += teks_bersih + " "
                
            if len(semua_teks_indo) > 100:
                data_sesi['preview_id'] = semua_teks_indo[:100] + '...'
            else:
                data_sesi['preview_id'] = semua_teks_indo
                
            if len(semua_teks_inggris) > 100:
                data_sesi['preview_en'] = semua_teks_inggris[:100] + '...'
            else:
                data_sesi['preview_en'] = semua_teks_inggris
        else:
            data_sesi['preview_id'] = '-'
            data_sesi['preview_en'] = '-'
            
        # Ubah data ke teks aman (Base64) biar gak error saat dikirim ke HTML
        isi_riwayat = data_sesi.get('riwayat', [])
        teks_json = json.dumps(isi_riwayat)
        data_sesi['riwayat_b64'] = base64.b64encode(teks_json.encode('utf-8')).decode('utf-8')
        
        isi_catatan = data_sesi.get('catatan', '')
        if isi_catatan == '':
            isi_catatan = 'Tidak ada catatan.'
        catatan_json = json.dumps(isi_catatan)
        data_sesi['catatan_b64'] = base64.b64encode(catatan_json.encode('utf-8')).decode('utf-8')
        
        isi_feedback = data_sesi.get('feedback_kepsek', '')
        feedback_json = json.dumps(isi_feedback)
        data_sesi['feedback_b64'] = base64.b64encode(feedback_json.encode('utf-8')).decode('utf-8')
        data_sesi['has_feedback'] = bool(isi_feedback)
            
        daftar_riwayat.append(data_sesi)
        
    # Urutkan secara manual untuk menggantikan order_by Firestore
    valid_riwayat = [r for r in daftar_riwayat if r.get('timestamp')]
    invalid_riwayat = [r for r in daftar_riwayat if not r.get('timestamp')]
    valid_riwayat.sort(key=lambda x: x['timestamp'], reverse=True)
    daftar_riwayat = valid_riwayat + invalid_riwayat
        
    return render_template('guru_riwayat.html', daftar_riwayat=daftar_riwayat, guru=guru_aktif)

@aplikasi.route('/guru/sesi')
def sesi_stt():
    logged_in = session.get('logged_in_user')  # type: ignore  # type: ignore
    guru_aktif = logged_in if isinstance(logged_in, dict) else {'nama': 'Guru Belum Ada', 'nip': '-'}
    
    kelas = []
    for doc in db.collection('kelas').stream():
        kelas.append(doc.to_dict())
        
    daftar_folder = []
    for k in kelas:
        if str(k.get('guru_pengajar', '')).lower() == str(guru_aktif.get('nama', '')).lower():
            daftar_folder.append(k)
            
    nama_kelas = request.args.get('nama_kelas', 'Sesi STT')
    kunci_deepgram = os.getenv('DEEPGRAM_API_KEY')
    return render_template('stt_session.html', deepgram_api_key=kunci_deepgram, nama_kelas=nama_kelas, daftar_folder=daftar_folder)

@aplikasi.route('/api/translate', methods=['POST'])
def api_translate():
    data_masuk = request.json
    if not data_masuk or 'text' not in data_masuk:
        return jsonify({'error': 'Tidak ada teks yang diisi'}), 400
    
    teks_asal = data_masuk['text']
    bahasa_tujuan = data_masuk.get('target', 'en')
    
    if translate_client is None:
        return jsonify({'error': 'Google Translate belum aktif.'}), 500
        
    try:
        hasil = translate_client.translate(
            teks_asal,
            target_language=bahasa_tujuan
        )
        return jsonify({'translation': hasil['translatedText']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@aplikasi.route('/api/simpan_riwayat', methods=['POST'])
def api_simpan_riwayat():
    data_masuk = request.json
    if not data_masuk or 'riwayat' not in data_masuk:
        return jsonify({'error': 'Data riwayat kosong'}), 400
    
    try:
        data_riwayat = data_masuk.get('riwayat', [])
        catatan_guru = data_masuk.get('catatan', '')
        nama_kelas = data_masuk.get('nama_kelas', 'Sesi STT')
        
        # Bikin dokumen baru di Firestore
        dokumen_baru = db.collection('riwayat_sesi').document()
        dokumen_baru.set({
            'nama_kelas': nama_kelas,
            'riwayat': data_riwayat,
            'catatan': catatan_guru,
            'timestamp': firestore.SERVER_TIMESTAMP
        })
        
        return jsonify({'success': True, 'id': dokumen_baru.id})
    except Exception as error_nya:
        return jsonify({'error': str(error_nya)}), 500

@aplikasi.route('/api/upload_foto_profil', methods=['POST'])
def api_upload_foto_profil():
    logged_in = session.get('logged_in_user')
    role = session.get('role')
    
    if not logged_in or not role:
        return jsonify({'error': 'Sesi tidak valid atau telah kedaluwarsa'}), 401
        
    if 'foto_profil' not in request.files:
        return jsonify({'error': 'Tidak ada file yang diunggah'}), 400
        
    file = request.files['foto_profil']
    if file.filename == '':
        return jsonify({'error': 'Nama file kosong'}), 400
        
    if file:
        # Validasi ekstensi file
        ext = file.filename.rsplit('.', 1)[-1].lower()
        if ext not in ['jpg', 'jpeg', 'png', 'webp', 'gif']:
            return jsonify({'error': 'Format file tidak didukung. Gunakan JPG, JPEG, PNG, WEBP, atau GIF.'}), 400
            
        user_id = logged_in.get('id')
        role_collection = 'admin' if role == 'admin' else ('kepsek' if role == 'kepala-sekolah' else 'guru')
        
        filename = f"{role}_{user_id}.{ext}"
        filepath = os.path.join(aplikasi.config['UPLOAD_PROFIL_FOLDER'], filename)
        
        # Simpan file ke direktori lokal
        file.save(filepath)
        
        try:
            # Update nama file foto di Firestore
            db.collection(role_collection).document(user_id).update({
                'foto_profil': filename
            })
            
            # Update data user di Flask session
            logged_in['foto_profil'] = filename
            session['logged_in_user'] = logged_in
            session.modified = True
            
            return jsonify({'success': True, 'filename': filename})
        except Exception as e:
            return jsonify({'error': f"Gagal memperbarui database: {str(e)}"}), 500

@aplikasi.route('/api/hapus_foto_profil', methods=['POST'])
def api_hapus_foto_profil():
    logged_in = session.get('logged_in_user')
    role = session.get('role')
    
    if not logged_in or not role:
        return jsonify({'error': 'Sesi tidak valid atau telah kedaluwarsa'}), 401
        
    user_id = logged_in.get('id')
    role_collection = 'admin' if role == 'admin' else ('kepsek' if role == 'kepala-sekolah' else 'guru')
    
    # Cek jika ada foto profil saat ini, hapus filenya
    current_foto = logged_in.get('foto_profil')
    if current_foto:
        filepath = os.path.join(aplikasi.config['UPLOAD_PROFIL_FOLDER'], current_foto)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
                
    try:
        # Update nama file foto di Firestore jadi None/null
        db.collection(role_collection).document(user_id).update({
            'foto_profil': None
        })
        
        # Update data user di Flask session
        logged_in['foto_profil'] = None
        session['logged_in_user'] = logged_in
        session.modified = True
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f"Gagal memperbarui database: {str(e)}"}), 500

@aplikasi.route('/api/simpan_feedback', methods=['POST'])
def api_simpan_feedback():
    data_masuk = request.json
    if not data_masuk or 'id' not in data_masuk or 'feedback' not in data_masuk:
        return jsonify({'error': 'Data tidak lengkap'}), 400
        
    try:
        riwayat_id = data_masuk.get('id')
        teks_feedback = data_masuk.get('feedback')
        
        db.collection('riwayat_sesi').document(riwayat_id).update({
            'feedback_kepsek': teks_feedback
        })
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def generate_manual_kata_fokus(kata, lang):
    # Dapatkan terjemahan menggunakan translate_client jika aktif
    arti = kata.lower()
    if translate_client:
        try:
            if lang == 'id':
                # Cari terjemahan dari ID ke EN
                hasil = translate_client.translate(kata, target_language='en')
                arti = hasil['translatedText']
            else:
                # Cari terjemahan dari EN ke ID
                hasil = translate_client.translate(kata, target_language='id')
                arti = hasil['translatedText']
        except Exception:
            pass
            
    if lang == 'id':
        spell = kata.lower()
        cara = spell
        catatan = f"Contoh penggunaan: Ayo gunakan kata '{kata.lower()}' dalam kalimat."
    else:
        spell = konversi_kata_en_ke_fonetik(kata)
        cara = spell
        catatan = f"Contoh penggunaan: I like this '{kata.lower()}' ({arti.lower()})."
        
    return {
        "word": kata,
        "spell": spell,
        "arti": arti.capitalize(),
        "cara": cara,
        "catatan": catatan
    }

@aplikasi.route('/api/generate_kata_fokus', methods=['POST'])
@aplikasi.route('/api/generate-kata-fokus', methods=['POST'])
def api_generate_kata_fokus():
    data_masuk = request.json
    if not data_masuk or 'word' not in data_masuk:
        return jsonify({'error': 'Kata belum dimasukkan'}), 400
        
    kata_dicari = data_masuk['word'].upper()
    lang = data_masuk.get('lang', 'en')
    doc_id = f"{kata_dicari}_{lang}"
    
    # 1. Cek dulu apakah kata ini sudah pernah dicari dan disimpan di database
    dokumen = db.collection('kamus_kosakata').document(doc_id).get()
    if dokumen.exists:
        data_cache = dokumen.to_dict()
        if data_cache.get('spell'):
            clean_spell = re.sub('<[^<]+>', '', data_cache['spell'])
            data_cache['spell'] = clean_spell.replace('-', '')
        if data_cache.get('cara'):
            clean_cara = re.sub('<[^<]+>', '', data_cache['cara'])
            data_cache['cara'] = clean_cara.replace('-', '')
        return jsonify(data_cache)
        
    # 2. Kalau belum ada, kita coba generate via AI terlebih dahulu (Dinamis: OpenAI -> Gemini)
    try:
        try:
            mesin_ai = UnifiedAIClient()
            if lang == 'id':
                perintah = f"""Tolong kasih penjelasan singkat kata bahasa Indonesia '{kata_dicari}' untuk anak SLB.
Balas HANYA pakai format JSON, jangan ada teks lain.

Aturan Ejaan Cara Baca ("spell" & "cara"):
- Berikan ejaan cara baca yang jelas.
- Gunakan huruf yang mudah dibaca.
- JANGAN gunakan tanda pemisah suku kata (-).
Contoh: bermain -> bermain, sekolah -> sekolah

Aturan JSON:
1. "word": Kata '{kata_dicari}' huruf besar semua.
2. "spell": Ejaan cara baca utuh.
3. "arti": Persamaan kata (sinonim) atau makna yang sangat sederhana.
4. "cara": Tulis ulang "spell".
5. "catatan": Satu kalimat contoh penggunaan kata yang sangat mudah untuk anak SLB.

Format JSON persis seperti ini:
{{
  "word": "{kata_dicari}",
  "spell": "bermain",
  "arti": "Bersenang-senang",
  "cara": "bermain",
  "catatan": "Ayo kita bermain bola di lapangan."
}}"""
            else:
                perintah = f"""Tolong kasih penjelasan singkat kata bahasa Inggris '{kata_dicari}' untuk anak SLB.
Balas HANYA pakai format JSON, jangan ada teks lain.

Aturan Ejaan Cara Baca ("spell" & "cara"):
- Ubah kata bahasa Inggris menjadi cara baca sederhana untuk anak SLB.
- Gunakan ejaan fonetik sederhana (bukan IPA).
- Gunakan huruf yang mudah dibaca dalam bahasa Indonesia.
- Hindari simbol linguistik yang rumit.
- Buat pengucapan sejelas mungkin.
- JANGAN gunakan tanda pemisah suku kata (-).
Contoh: how → hau, today → tudei, thank → theng, you → yu

Aturan JSON:
1. "word": Kata '{kata_dicari}' huruf besar semua.
2. "spell": Ejaan cara baca sesuai aturan di atas.
3. "arti": Terjemahan bahasa Indonesia.
4. "cara": Tulis ulang "spell" yang sama dengan yang di atas.
5. "catatan": Satu kalimat penjelasan gampang buat anak SLB.

Format JSON persis seperti ini:
{{
  "word": "{kata_dicari}",
  "spell": "hau",
  "arti": "Bagaimana",
  "cara": "hau",
  "catatan": "Kata ini dipakai untuk menanyakan sesuatu."
}}"""

            jawaban_ai = mesin_ai.generate_content(perintah)
            teks_jawaban = jawaban_ai.text.strip()
            
            # Potong teks supaya pas cuma ambil JSON-nya saja
            awal = teks_jawaban.find('{')
            akhir = teks_jawaban.rfind('}')
            if awal != -1 and akhir != -1:
                teks_jawaban = teks_jawaban[awal:akhir+1]
                data_jadi = json.loads(teks_jawaban)
                ejaan = data_jadi.get('spell', '')
                data_jadi['spell'] = ejaan.replace('-', '')
                
                # Simpan ke database
                db.collection('kamus_kosakata').document(doc_id).set(data_jadi)
                return jsonify(data_jadi)
        except Exception as e_ai:
            print("API AI gagal/limit habis, menggunakan fallback manual untuk kata fokus:", e_ai)
            
        # Fallback manual jika AI error
        data_jadi = generate_manual_kata_fokus(kata_dicari, lang)
        db.collection('kamus_kosakata').document(doc_id).set(data_jadi)
        return jsonify(data_jadi)
        
    except Exception as error_nya:
        print("Error total generate kata fokus:", error_nya)
        try:
            data_jadi = generate_manual_kata_fokus(kata_dicari, lang)
            return jsonify(data_jadi)
        except Exception as e_fallback:
            return jsonify({'error': str(e_fallback)}), 500

# Kamus ejaan bahasa Inggris -> cara baca Indonesia (lokal fallback)
KAMUS_EJAAN_EN = {
    # Singkatan umum
    "let's": "lets", "lets": "lets",
    "it's": "its", "its": "its",
    "that's": "dets", "thats": "dets",
    "i'm": "aim", "im": "aim",
    "don't": "dount", "dont": "dount",
    "can't": "kent", "cant": "kent",
    "won't": "wount", "wont": "wount",
    "you're": "yur", "youre": "yur",
    "we're": "wir", "were": "wer",
    "they're": "deir", "theyre": "deir",
    "he's": "his", "hes": "his",
    "she's": "syis", "shes": "syis",
    "what's": "wots", "whats": "wots",
    "there's": "ders", "theres": "ders",
    "here's": "hirs", "heres": "hirs",

    # Singkatan n't tambahan
    "doesn't": "dazen", "doesnt": "dazen",
    "didn't": "diden", "didnt": "diden",
    "isn't": "izen", "isnt": "izen",
    "aren't": "aren", "arent": "aren",
    "wasn't": "wozen", "wasnt": "wozen",
    "weren't": "weren", "werent": "weren",
    "haven't": "heven", "havent": "heven",
    "hasn't": "hezen", "hasnt": "hezen",
    "hadn't": "heden", "hadnt": "heden",
    "couldn't": "kuden", "couldnt": "kuden",
    "shouldn't": "syuden", "shouldnt": "syuden",
    "wouldn't": "wuden", "wouldnt": "wuden",
    "mustn't": "masen", "mustnt": "masen",

    # Kata Ganti (Pronouns)
    "i": "ai", "me": "mi", "my": "mai", "myself": "mai self",
    "you": "yu", "your": "yor", "yours": "yors", "yourself": "yorself",
    "he": "hi", "him": "him", "his": "his", "himself": "himself",
    "she": "syi", "her": "her", "hers": "hers", "herself": "herself",
    "it": "it", "its": "its", "itself": "itself",
    "we": "wi", "us": "as", "our": "aur", "ours": "aurs", "ourselves": "aur selvz",
    "they": "dei", "them": "dem", "their": "deir", "theirs": "deirs", "themselves": "deir selvz",
    "everyone": "evri wan", "everybody": "evri badi", "someone": "sam wan", "anyone": "eni wan",
    
    # Kata Tanya (Questions)
    "what": "wot", "who": "hu", "whom": "hum", "whose": "huz",
    "where": "wer", "when": "wen", "why": "wai", "how": "hau",
    
    # Kata Kerja Bantu & to be
    "am": "em", "is": "is", "are": "ar", "was": "wos", "were": "wer",
    "be": "bi", "been": "bin", "being": "bi-ing",
    "have": "hev", "has": "hes", "had": "hed",
    "do": "du", "does": "das", "did": "did", "done": "dan", "doing": "du-ing",
    "can": "ken", "could": "kud", "shall": "syal", "should": "syud",
    "will": "wil", "would": "wud", "may": "mei", "might": "mait", "must": "mas",
    
    # Angka & Waktu
    "one": "wan", "two": "tu", "three": "tri", "four": "for", "five": "faif",
    "six": "siks", "seven": "seven", "eight": "eit", "nine": "nain", "ten": "ten",
    "today": "tu dei", "tomorrow": "tu morou", "yesterday": "yes ter dei",
    "morning": "mor ning", "afternoon": "af ter nun", "evening": "if ning", "night": "nait",
    "day": "dei", "week": "wik", "month": "manth", "year": "yir", "yet": "yet",
    
    # Percakapan Umum & Kata Sifat
    "hello": "helo", "hi": "hai", "please": "plis", "thanks": "tengks", "thank": "tengk",
    "welcome": "wel kam", "sorry": "sori", "excuse": "eks kius", "yes": "yes", "no": "nou",
    "good": "gud", "bad": "bed", "fine": "fain", "nice": "nais", "great": "greit",
    "well": "wel", "easy": "izi", "hard": "hard", "busy": "bizi",
    
    # Kata Depan & Hubung
    "the": "de", "a": "e", "an": "en", "and": "end", "but": "bat", "or": "or",
    "in": "in", "on": "on", "at": "et", "to": "tu", "for": "for", "with": "wit",
    "from": "from", "by": "bai", "about": "e baut", "of": "of", "off": "of",
    "this": "dis", "that": "det", "these": "diz", "those": "douz",
    "here": "hir", "there": "der", "some": "sam", "any": "eni",
    
    # Kosakata Sekolah & Akademik (Akurasi Tinggi)
    "english": "ing glis", "indonesia": "in do ne sya", "school": "skul", "class": "klas",
    "classroom": "klas rum", "teacher": "tit ser", "student": "stu den", "book": "buk",
    "pen": "pen", "pencil": "pen sil", "paper": "peiper", "notebook": "nout buk",
    "read": "rid", "write": "rait", "writes": "raits", "wrote": "rout", "written": "riten",
    "speak": "spik", "listen": "lisen", "learn": "lern", "learns": "lerns", "learned": "lernd",
    "study": "stadi", "teach": "tic", "lesson": "leson", "homework": "houm wirk",
    "question": "kwes syen", "answer": "en ser", "word": "werd", "sentence": "sen tens",
    
    # Kata Kerja Umum Khusus (Akurasi Tinggi dari user request)
    "give": "gif", "gives": "gifs", "gave": "geif", "given": "given", "giving": "gifting",
    "understand": "ander stend", "understands": "ander stends", "understood": "ander stud", "understanding": "ander stending",
    "start": "start", "started": "star ted", "starting": "starting",
    "look": "luk", "looks": "luks", "looking": "luking", "looked": "lukt",
    "like": "laik", "likes": "laiks", "liking": "laiking", "liked": "laikt",
    "image": "imej", "images": "imejiz", "media": "midia", "medias": "midia",
    "budget": "bajet", "budgets": "bajets", "facility": "fasiliti", "facilities": "fasilitiz",
    "people": "pipel", "peoples": "pipels",
}

def konversi_kata_en_ke_fonetik(kata):
    kata_lower = kata.lower()
    
    # 1. Cek kamus pemetaan kata langsung
    if kata_lower in KAMUS_EJAAN_EN:
        return KAMUS_EJAAN_EN[kata_lower]
        
    # 2. Jika tidak ada di kamus, gunakan aturan konversi huruf (fonetis)
    ejaan = kata_lower
    
    # a. Ganti akhiran -tion ke -syen
    ejaan = re.sub(r'tion\b', 'syen', ejaan)
    ejaan = re.sub(r'tions\b', 'syenz', ejaan)
    
    # b. Penanganan a_konsonan_e -> ei (name -> neim, game -> geim)
    ejaan = re.sub(r'a([bcdfghjklmnpqrstvwxyz])e\b', r'ei\1', ejaan)
    # c. Penanganan i_konsonan_e -> ai (like -> laik, time -> taim)
    ejaan = re.sub(r'i([bcdfghjklmnpqrstvwxyz])e\b', r'ai\1', ejaan)
    # d. Penanganan o_konsonan_e -> ou (home -> houm, close -> klous)
    ejaan = re.sub(r'o([bcdfghjklmnpqrstvwxyz])e\b', r'ou\1', ejaan)
    
    # e. Ganti sh -> sy, ch -> c, ph -> f, ck -> k
    ejaan = ejaan.replace('sh', 'sy')
    ejaan = ejaan.replace('ch', 'c')
    ejaan = ejaan.replace('ph', 'f')
    ejaan = ejaan.replace('ck', 'k')
    
    # f. Penanganan double vowels: ee -> i, ea -> i, oo -> u
    ejaan = ejaan.replace('ee', 'i')
    ejaan = ejaan.replace('ea', 'i')
    ejaan = ejaan.replace('oo', 'u')
    
    # g. Penanganan 'th' -> 't' (think -> tingk, thank -> tengk)
    ejaan = ejaan.replace('th', 't')
    
    # h. Penanganan akhiran 'y' pada kata pendek -> 'ai' (try -> trai, fly -> flai)
    if len(ejaan) <= 4 and ejaan.endswith('y'):
        ejaan = ejaan[:-1] + 'ai'
    # Penanganan akhiran 'y' pada kata panjang -> 'i' (happy -> hepi, family -> famili)
    elif ejaan.endswith('y'):
        ejaan = ejaan[:-1] + 'i'
        
    # i. Penanganan 'c' diikuti e, i, y -> 's'
    ejaan = re.sub(r'c([eiy])', r's\1', ejaan)
    # 'c' lainnya -> 'k'
    ejaan = ejaan.replace('c', 'k')
    
    # j. Penanganan 'x' -> 'ks'
    ejaan = ejaan.replace('x', 'ks')
    
    # k. Hilangkan akhiran 'e' bisu di akhir kata jika panjang kata > 3
    if len(ejaan) > 3 and ejaan.endswith('e') and not ejaan.endswith(('ae', 'ee', 'ie', 'oe', 'ue')):
        ejaan = ejaan[:-1]

    # Penyelarasan vokal minor
    # 'ou' -> 'au' (house -> haus, out -> aut)
    ejaan = ejaan.replace('ou', 'au')
    # 'ow' -> 'au' (how -> hau, now -> nau)
    ejaan = ejaan.replace('ow', 'au')
    
    return ejaan

def generate_manual_phonetics(teks, lang):
    import re
    
    # 1. Normalisasi teks jika bahasa Inggris
    if lang == 'en':
        # Ubah curly apostrophe menjadi tegak
        teks = teks.replace("’", "'").replace("‘", "'")
        
        # Gabungkan singkatan bahasa Inggris yang terpisah spasi secara umum
        singkatan = {
            r"\blet\s+s\b": "let's",
            r"\bit\s+s\b": "it's",
            r"\bthat\s+s\b": "that's",
            r"\bhe\s+s\b": "he's",
            r"\bshe\s+s\b": "she's",
            r"\bi\s+m\b": "i'm",
            r"\bdon\s+t\b": "don't",
            r"\bcan\s+t\b": "can't",
            r"\bwon\s+t\b": "won't",
            r"\byou\s+re\b": "you're",
            r"\bwe\s+re\b": "we're",
            r"\bthey\s+re\b": "they're",
            r"\bwhat\s+s\b": "what's",
            r"\bwho\s+s\b": "who's",
            r"\bwhere\s+s\b": "where's",
            r"\bwhen\s+s\b": "when's",
            r"\bwhy\s+s\b": "why's",
            r"\bhow\s+s\b": "how's",
            r"\bthere\s+s\b": "there's",
            r"\bhere\s+s\b": "here's",
            r"\bdoesn\s+t\b": "doesn't",
            r"\bdidn\s+t\b": "didn't",
            r"\bisn\s+t\b": "isn't",
            r"\baren\s+t\b": "aren't",
            r"\bwasn\s+t\b": "wasn't",
            r"\bweren\s+t\b": "weren't",
            r"\bhaven\s+t\b": "haven't",
            r"\bhasn\s+t\b": "hasn't",
            r"\bhadn\s+t\b": "hadn't",
            r"\bcouldn\s+t\b": "couldn't",
            r"\bshouldn\s+t\b": "shouldn't",
            r"\bwouldn\s+t\b": "wouldn't",
            r"\bmustn\s+t\b": "mustn't"
        }
        for pola, pengganti in singkatan.items():
            teks = re.sub(pola, pengganti, teks, flags=re.IGNORECASE)
            
    # Bersihkan tanda baca
    words = re.findall(r"\b[a-zA-Z']+\b", teks)
    if not words:
        return [{"phrase": teks, "phonetic": teks.lower()}]
        
    phonetics_array = []
    
    # Daftar kata hubung pemecah frasa
    conjunctions = {"and", "but", "or", "so", "because", "with", "from", "to", "for", "in", "on", "at"}
    
    current_phrase_words = []
    
    for word in words:
        is_break = False
        if word.lower() in conjunctions:
            is_break = True
        elif len(current_phrase_words) >= 3: # maksimal 3 kata per frasa agar garis kuning proporsional
            is_break = True
            
        if is_break and current_phrase_words:
            phrase = " ".join(current_phrase_words)
            if lang == 'id':
                phonetic = phrase.lower()
            else:
                phonetic = " ".join([konversi_kata_en_ke_fonetik(w) for w in current_phrase_words])
            phonetics_array.append({"phrase": phrase, "phonetic": phonetic})
            current_phrase_words = []
            
        current_phrase_words.append(word)
        
    if current_phrase_words:
        phrase = " ".join(current_phrase_words)
        if lang == 'id':
            phonetic = phrase.lower()
        else:
            phonetic = " ".join([konversi_kata_en_ke_fonetik(w) for w in current_phrase_words])
        phonetics_array.append({"phrase": phrase, "phonetic": phonetic})
        
    return phonetics_array

@aplikasi.route('/api/generate_phonetics_sentence', methods=['POST'])
@aplikasi.route('/api/generate-phonetics-sentence', methods=['POST'])
def api_generate_phonetics_sentence():
    data_masuk = request.json
    if not data_masuk or 'text' not in data_masuk:
        return jsonify({'error': 'Teks belum dimasukkan'}), 400
        
    teks = data_masuk['text']
    lang = data_masuk.get('lang', 'en')
    
    try:
        import hashlib
        # Gunakan hash MD5 dari teks dan bahasa sebagai ID dokumen
        doc_id = hashlib.md5(f"{teks}_{lang}".encode('utf-8')).hexdigest()
        
        # 1. Cek dulu apakah kalimat ini sudah pernah diproses di DB
        dokumen = db.collection('kamus_kalimat').document(doc_id).get()
        if dokumen.exists:
            data_cache = dokumen.to_dict()
            phonetics_array = data_cache.get('phonetics_array', [])
            for item in phonetics_array:
                if item.get('phonetic'):
                    clean_phonetic = re.sub('<[^<]+>', '', item['phonetic'])
                    item['phonetic'] = clean_phonetic.replace('-', '')
            return jsonify({'phonetics_array': phonetics_array})

        # 2. Coba gunakan AI terlebih dahulu (Dinamis: OpenAI -> Gemini)
        try:
            mesin_ai = UnifiedAIClient()
            
            if lang == 'id':
                perintah = f"""Tolong kelompokkan kalimat bahasa Indonesia berikut ke dalam frasa (bagian kalimat) yang bermakna, lalu berikan cara bacanya per frasa untuk anak SLB:
Kalimat: "{teks}"

Aturan:
1. Bagi kalimat menjadi frasa-frasa pendek yang bermakna (contoh: "Halo", "apa kabar").
2. Berikan cara baca untuk setiap kata dalam frasa, pisahkan antar kata dengan spasi (JANGAN gunakan tanda strip '-').
3. Balas HANYA dengan format JSON Array persis seperti contoh. Jangan ada teks lain.
Contoh: "Bapak pergi ke pasar" menjadi:
[
  {{"phrase": "Bapak pergi", "phonetic": "bapak pergi"}},
  {{"phrase": "ke pasar", "phonetic": "ke pasar"}}
]
"""
            else:
                perintah = f"""Tolong kelompokkan kalimat bahasa Inggris berikut ke dalam frasa (bagian kalimat) yang bermakna, lalu berikan cara bacanya menggunakan ejaan fonetik bahasa Indonesia agar mudah dieja oleh anak SLB:
Kalimat: "{teks}"

Aturan:
1. Bagi kalimat menjadi frasa-frasa pendek yang bermakna (contoh: "Hello", "how are you").
2. Ubah pengucapan bahasa Inggris menjadi tulisan sesuai cara orang Indonesia membacanya. Pisahkan antar kata dengan spasi (JANGAN gunakan tanda strip '-').
3. Balas HANYA dengan format JSON Array persis seperti contoh. Jangan ada teks lain.
Contoh: "Hello how are you" menjadi:
[
  {{"phrase": "Hello", "phonetic": "helo"}},
  {{"phrase": "how are you", "phonetic": "hau ar yu"}}
]
"""
            
            jawaban_ai = mesin_ai.generate_content(perintah)
            teks_jawaban = jawaban_ai.text.strip()
            
            # Ekstrak JSON
            awal = teks_jawaban.find('[')
            akhir = teks_jawaban.rfind(']')
            if awal != -1 and akhir != -1:
                teks_jawaban = teks_jawaban[awal:akhir+1]
                data_array = json.loads(teks_jawaban)
                
                for item in data_array:
                    ejaan_lama = item.get('phonetic', '')
                    item['phonetic'] = ejaan_lama.replace('-', '')
                    
                # Simpan hasil AI ke Firestore
                db.collection('kamus_kalimat').document(doc_id).set({'phonetics_array': data_array})
                return jsonify({'phonetics_array': data_array})
        except Exception as e_ai:
            print("API AI gagal/limit habis, menggunakan fallback manual untuk ejaan kalimat:", e_ai)

        # Fallback offline jika AI error
        data_array = generate_manual_phonetics(teks, lang)
        db.collection('kamus_kalimat').document(doc_id).set({'phonetics_array': data_array})
        return jsonify({'phonetics_array': data_array})
        
    except Exception as error_nya:
        print("Error total generate phonetic:", error_nya)
        # Fallback langsung jika Firestore/DB juga error
        try:
            data_array = generate_manual_phonetics(teks, lang)
            return jsonify({'phonetics_array': data_array})
        except Exception as e_fallback:
            return jsonify({'error': str(e_fallback)}), 500

def generate_manual_kuis_game(words, lang):
    import random
    pertanyaan = []
    
    # Kumpulkan terjemahan untuk semua kata terlebih dahulu
    kamus_terjemahan = {}
    for w in words:
        terjemahan = w.lower()
        if translate_client:
            try:
                target = 'id' if lang != 'id' else 'en'
                hasil = translate_client.translate(w, target_language=target)
                terjemahan = hasil['translatedText']
            except Exception:
                pass
        kamus_terjemahan[w] = terjemahan.capitalize()
        
    # Daftar semua arti untuk dijadikan pilihan pengecoh
    semua_arti = list(kamus_terjemahan.values())
    
    # Kata pengecoh tambahan jika koleksi kata terlalu sedikit
    pengecoh_tambahan_id = ["Kucing", "Rumah", "Buku", "Sekolah", "Makan", "Minum", "Tidur", "Lari", "Duduk", "Guru"]
    pengecoh_tambahan_en = ["Cat", "House", "Book", "School", "Eat", "Drink", "Sleep", "Run", "Sit", "Teacher"]
    
    for kata, arti_benar in kamus_terjemahan.items():
        # Buat daftar pilihan salah
        pilihan_salah = [a for a in semua_arti if a != arti_benar]
        
        # Jika pilihan salah kurang dari 3, tambahkan dari pengecoh tambahan
        cadangan = pengecoh_tambahan_id if lang != 'id' else pengecoh_tambahan_en
        random.shuffle(cadangan)
        for c in cadangan:
            if len(pilihan_salah) >= 3:
                break
            if c != arti_benar and c not in pilihan_salah:
                pilihan_salah.append(c)
                
        # Ambil tepat 3 pilihan salah
        pilihan_salah = pilihan_salah[:3]
        
        # Gabung jawaban benar dengan 3 pilihan salah
        pilihan = pilihan_salah + [arti_benar]
        random.shuffle(pilihan)
        
        pertanyaan.append({
            "kata": kata.upper(),
            "jawabanBenar": arti_benar,
            "pilihan": pilihan
        })
        
    return pertanyaan

@aplikasi.route('/api/generate_kuis_game', methods=['POST'])
@aplikasi.route('/api/generate-kuis-game', methods=['POST'])
def api_generate_kuis_game():
    data_masuk = request.json
    if not data_masuk or 'words' not in data_masuk:
        return jsonify({'error': 'Daftar kata belum dimasukkan'}), 400
        
    daftar_kata = data_masuk['words']
    lang = data_masuk.get('lang', 'en')
    if not isinstance(daftar_kata, list):
        return jsonify({'error': 'Format kata harus berupa list/array'}), 400
        
    try:
        # Coba gunakan AI terlebih dahulu (Dinamis: OpenAI -> Gemini)
        try:
            mesin_ai = UnifiedAIClient()
            kata_str = ", ".join(daftar_kata)
            
            if lang == 'id':
                perintah = f"""Buatkan soal latihan kosakata bahasa Inggris berdasarkan daftar kata bahasa Indonesia berikut: {kata_str}.
Target pengguna: Anak SLB (Sekolah Luar Biasa) yang sedang belajar bahasa Inggris dasar.

Aturan:
1. Terjemahkan setiap kata bahasa Indonesia tersebut ke bahasa Inggris. Jadikan terjemahan bahasa Inggris tersebut sebagai 'kata' soal (huruf kapital semua).
2. Jadikan arti aslinya dalam bahasa Indonesia sebagai jawaban benar (jawabanBenar).
3. Buatkan 3 pilihan jawaban salah (pengecoh) dalam bahasa Indonesia. Total harus ada tepat 4 pilihan jawaban yang terdiri dari 1 jawaban benar dan 3 jawaban salah.
4. Acak urutan "pilihan" agar jawaban benar tidak selalu di urutan yang sama.
5. JANGAN berikan penjelasan atau teks tambahan.
6. Balas HANYA dengan format JSON Array persis seperti contoh di bawah.

Contoh format balasan:
[
  {{
    "kata": "SLEEP",
    "jawabanBenar": "Tidur",
    "pilihan": ["Makan", "Lari", "Tidur", "Minum"]
  }}
]"""
            else:
                perintah = f"""Buatkan soal latihan kosakata untuk kata-kata bahasa Inggris berikut: {kata_str}.
Target pengguna: Anak SLB (Sekolah Luar Biasa) yang sedang belajar bahasa Inggris dasar.

Aturan:
1. Untuk setiap kata bahasa Inggris, berikan terjemahan bahasa Indonesianya yang paling sederhana sebagai jawaban benar (jawabanBenar).
2. Buatkan 3 pilihan jawaban salah (pengecoh) dalam bahasa Indonesia. Total harus ada tepat 4 pilihan jawaban yang terdiri dari 1 jawaban benar dan 3 jawaban salah.
3. Acak urutan "pilihan" agar jawaban benar tidak selalu di urutan yang sama.
4. JANGAN berikan penjelasan atau teks tambahan.
5. Balas HANYA dengan format JSON Array persis seperti contoh di bawah.

Contoh format balasan:
[
  {{
    "kata": "APPLE",
    "jawabanBenar": "Apel",
    "pilihan": ["Jeruk", "Apel", "Pisang", "Mangga"]
  }},
  {{
    "kata": "RUN",
    "jawabanBenar": "Lari",
    "pilihan": ["Tidur", "Makan", "Lari", "Duduk"]
  }}
]"""

            jawaban_ai = mesin_ai.generate_content(perintah)
            teks_jawaban = jawaban_ai.text.strip()
            
            awal = teks_jawaban.find('[')
            akhir = teks_jawaban.rfind(']')
            if awal != -1 and akhir != -1:
                teks_jawaban = teks_jawaban[awal:akhir+1]
                data_array = json.loads(teks_jawaban)
                return jsonify({'pertanyaan': data_array})
        except Exception as e_ai:
            print("API AI gagal/limit habis, menggunakan fallback manual untuk kuis game:", e_ai)

        # Generate latihan kuis secara offline manual (lebih cepat dan stabil)
        data_array = generate_manual_kuis_game(daftar_kata, lang)
        return jsonify({'pertanyaan': data_array})
        
    except Exception as error_nya:
        print("Error total generate kuis game:", error_nya)
        try:
            data_array = generate_manual_kuis_game(daftar_kata, lang)
            return jsonify({'pertanyaan': data_array})
        except Exception as e_fallback:
            return jsonify({'error': str(e_fallback)}), 500

@aplikasi.route('/api/update_teks_realtime', methods=['POST'])
def update_teks_realtime():
    data_masuk = request.json
    
    if not data_masuk or 'teks' not in data_masuk:
        return jsonify({'error': 'Tidak ada teks yang dikirim'}), 400
         
    teks_baru = data_masuk['teks']
    
    try:
        ref = rtdb.reference('teks_realtime')
        ref.set(teks_baru)
        return jsonify({'success': True, 'pesan': 'Teks realtime berhasil diupdate di Firebase!'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@aplikasi.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('halaman_utama'))

if __name__ == '__main__':
    aplikasi.run(debug=True, port=5000)
