# Analisis & Roadmap Rekomendasi Improvement Project BridgeA
**Disusun oleh Fleet Multi-Agent Orion (FORGE, SENTINEL, FRAME, AURORA, ATLAS, PRISM, ORION)**  
**Repository:** `https://github.com/IndraYuda13/BridgeA`  
**Path Lokal:** `/root/projects/BridgeA`  
**Tanggal:** September 2026

---

## 1. Executive Summary & Ringkasan Kondisi Saat Ini

BridgeA adalah platform web inklusif berbasis Python Flask yang dirancang untuk mendukung pembelajaran siswa tunarungu/SLB (Sekolah Luar Biasa) melalui integrasi:
* **Speech-to-Text (STT) Real-time** via Deepgram WebSocket (Nova-2).
* **AI Assistive Learning** via Google Gemini (ekstraksi kata fokus, cara baca fonetik ramah SLB, generator kuis game).
* **Multi-Role Dashboards** (Admin, Kepala Sekolah, Guru).
* **WhatsApp OTP Recovery** via Fonnte API.

Berdasarkan audit komprehensif 6 spesialis Fleet (FORGE, SENTINEL, FRAME, AURORA, ATLAS, PRISM), aplikasi saat ini memiliki konsep produk yang sangat berdampak sosial, namun implementasi teknisnya berada pada status **Prototipe Awal Berisiko Tinggi (Critical Technical Debt)**. 

### Temuan Utama Lintas Domain:
1. **Keamanan Kritikal (P0):** Password tersimpan plaintext di Firestore, celah *Account Takeover* 100% pada fitur lupa sandi akun kosong, kebocoran master key Deepgram ke browser client, dan tidak ada otorisasi pada seluruh endpoint `/api/*`.
2. **Arsitektur Monolitik Ekstrem (P1):** `app.py` menumpuk 1.710 baris kode tanpa Blueprints/Service layer. Template `stt_session.html` menumpuk 1.615 baris (CSS + JS + HTML inline).
3. **Database & Performa (P1):** Kueri Firestore menggunakan full-collection stream tanpa index, relasi entitas memakai raw string nama bukan ID/foreign key, dan update teks realtime memicu HTTP flooding yang memblokir thread synchronous Flask.
4. **DevOps & Container (P1):** Foto profil disimpan ke disk container lokal (hilang saat restart di cloud), dependensi unpinned memicu backtracking pip, dan tidak ada health check.
5. **Frontend & UX SLB (P2):** CSS dashboard rusak di mobile/tablet (0 media query, `overflow:hidden`), delay jeda bicara 5 detik terlalu lambat untuk siswa tunarungu, teks interim tidak memenuhi rasio kontras WCAG AA, dan belum ada integrasi visual artikulasi bibir (*viseme*).
6. **QA & Pengujian (P2):** 0% test coverage, tidak ada test runner, dan tight coupling yang menghalangi unit testing.

---

## 2. Analisis Spesialis Per Agent

### A. FORGE (Backend, Systems & Database Architecture)
* **Monolith Coupling:** 
  `app.py` memuat 37 endpoint, inisialisasi Firebase/GCP global, algoritma kuis, parsing string, kamus statis raksasa `KAMUS_EJAAN_EN` (lines 1214-1398). Tidak ada pola Blueprint, Service, maupun Repository pattern.
* **Firestore Inefficiencies:**
  * Endpoint `/api/cek_wa` (lines 306-328) men-stream seluruh koleksi `guru`, `kepsek`, `admin` untuk mencocokkan NIP di memory Python ($O(N)$ reads).
  * Menghindari Composite Index di Firestore (line 821) dengan menarik semua dokumen ke RAM lalu melakukan sorting Python `valid_riwayat.sort()` (lines 900-903).
  * Folder riwayat guru diakali dengan membuat dokumen kelas fiktif di koleksi `kelas` (`jadwal_hari: 'TBA'`), merusak konsistensi skema.
* **In-Memory Cache Drift:**
  `_cache = {}` (lines 76-97) terisolasi per worker Gunicorn. Dengan 2 worker process, mutasi di Worker 1 tidak menghapus cache di Worker 2 (split-brain cache hingga 300 detik).
* **Thread Starvation via RTDB Sync:**
  Browser mengirim HTTP POST berfrekuensi tinggi ke `/api/update_teks_realtime` (line 1688) yang melakukan blocking synchronous write ke Firebase RTDB. 8 thread Gunicorn cepat habis, membuat aplikasi macet untuk user lain.
* **Integrasi Eksternal:**
  * Model Gemini menggunakan string `gemini-2.5-flash` (versi non-standar) dan library `google.generativeai` sudah deprecated oleh Google (disarankan migrasi ke SDK baru `google-genai`).
  * Request Fonnte WA (`requests.post()`, line 123) tidak memiliki konfigurasi timeout (berisiko hang hingga 120 detik).

---

### B. SENTINEL (Security, Pentesting & Threat Modeling)
* **Matriks Kerentanan Kritis:**
  * **VULN-01 (CRITICAL - CVSS 9.8): Account Takeover via Lupa Sandi.** Pada `app.py` lines 320-325, jika akun admin/kepsek belum memiliki field `no_wa`, sistem mengizinkan input nomor WA penyerang dan mengirim OTP ke nomor tersebut. Penyerang dapat mereset sandi Admin secara instan.
  * **VULN-02 (CRITICAL - CVSS 9.1): Plaintext Passwords.** Kueri Firestore menggunakan `where('password', '==', password)` (lines 170, 195, 220). Tidak ada hashing (bcrypt/argon2).
  * **VULN-03 (CRITICAL - CVSS 9.8): Hardcoded Secret Key Fallback.** Fallback `kunci_rahasia_bridgea_default` (line 33) memungkinkan pemalsuan cookie sesi Flask (`__session`) menggunakan `flask-unsign` untuk eskalasi privilege ke Admin.
  * **VULN-04 (CRITICAL - CVSS 9.1): Broken Access Control Rute `/api/*`.** Hook `before_request` (lines 140-158) hanya membatasi prefix `/admin`, `/kepsek`, `/guru`. Endpoint `/api/simpan_riwayat`, `/api/simpan_feedback`, `/api/update_teks_realtime`, dan `/api/translate` terbuka untuk publik tanpa login.
  * **VULN-05 (HIGH - CVSS 7.5): Kebocoran Master Deepgram API Key.** `DEEPGRAM_API_KEY` dikirim ke template HTML (`stt_session.html:746`) dan dipakai langsung di JavaScript browser. Siapa saja dapat mencuri key untuk menyedot kuota API.
  * **VULN-06 (HIGH - CVSS 8.1): IDOR Hapus Modul & Kelas.** Endpoint `/guru/modul/hapus/<id>` dan `/guru/hapus_folder/<id>` tidak memvalidasi ownership pembuat. Guru A dapat menghapus modul/kelas milik Guru B.
  * **VULN-07 (HIGH - CVSS 7.5): OTP Brute Force.** Kode OTP hanya 4 digit (`random.randint(1000, 9999)`), tanpa limit percobaan salah di server, dan tidak menggunakan secure random (`secrets`).
  * **VULN-08 (HIGH - CVSS 8.2): Stored XSS pada Riwayat.** Konten riwayat dari `/api/simpan_riwayat` dirender langsung menggunakan `innerHTML` di `guru_riwayat.html` (lines 180-202) tanpa sanitasi HTML.
  * **VULN-09 (HIGH - CVSS 8.1): Ketiadaan Proteksi CSRF.** Tidak ada token anti-CSRF pada seluruh formulir mutasi data.

---

### C. FRAME (Frontend Engineering, Streaming & Responsiveness)
* **Monolitik Template:** `stt_session.html` berukuran 1.615 baris (504 baris CSS inline, 871 baris JS inline). Seluruh state disimpan di window global scope tanpa encapsulation.
* **Audio & WebSocket Handling:**
  * Izin mic dipanggil langsung saat `window.load` tanpa user gesture (diblokir otomatis oleh Safari iOS dan Chrome Android modern).
  * MediaRecorder hardcoded `audio/webm` tanpa fallback check (crash `NotSupportedError` di Safari iPad/iPhone).
  * WebSocket Deepgram tidak memiliki logika auto-reconnect atau error recovery; putus sedikit langsung alert popup.
* **DOM Thrashing & CLS:**
  * String concatenation langsung ke `.innerHTML` setiap interim chunk suara memicu layout reflow berulang kali.
  * Kartu teks tidak memiliki fixed/min height, memicu Cumulative Layout Shift (CLS) parah saat kalimat memanjang.
* **Latensi Artifisial 5 Detik:**
  `timerJedaKalimat` sengaja menunggu hening 5.000 ms sebelum memanggil translate dan AI. Ditambah response time API, total jeda mencapai 8-10 detik—sangat memutus konteks bagi siswa tunarungu.
* **Responsiveness Rusak:**
  `dashboard.css` (591 baris) dan `style.css` (412 baris) memiliki **0 media query**. Body terkunci `overflow: hidden; height: 100vh;` dan sidebar kaku 260px membuat dashboard terpotong dan tidak bisa di-scroll di layar HP/tablet.
* **Kelemahan Single-Tab Script:**
  Script `sessionStorage.getItem('tab_session_active')` di `<head>` merusak navigasi normal (misal: "Open in new tab" langsung ditendang ke login), tetapi sangat mudah di-bypass dengan mematikan JavaScript.

---

### D. AURORA (UX, Inclusivity & Classroom Ergonomics)
* **Kontras Warna di Bawah Standar WCAG AA:**
  * Teks *interim* realtime menggunakan warna `#9ca3af` di atas kartu putih (rasio kontras 2.5:1, gagal standar WCAG AA 4.5:1). Teks terlihat pudar dan kabur di layar proyektor kelas.
  * Garis bawah fonetik kuning muda `#fcd34d` tidak terlihat kontras di latar terang.
* **Jebakan Resolusi Proyektor Kelas (XGA 1024x768):**
  Breakpoint di `stt_session.html` disetel pada `@media (max-width: 1024px)`. Sebagian besar proyektor sekolah menggunakan resolusi XGA 1024x768. Akibatnya, tampilan 2 kolom runtuh menjadi 1 kolom vertikal panjang (>1400px), memaksa guru bolak-balik scrolling.
* **Ketiadaan High-Contrast Dark Mode:**
  Latar putih rentan mengalami *washout* akibat pencahayaan ruang kelas. Belum tersedia mode gelap kontras tinggi (latar `#0B0F17`, teks `#FFFFFF` rasio 18:1) untuk mempertajam keterbacaan dari jarak jauh.
* **Keterbatasan Fonetik Abjad Latin:**
  Ejaan teks Latin (contoh: "HOW" -> "Cara Baca: hau") bersifat abstrak bagi siswa tunarungu pre-lingual. Diperlukan representasi visual artikulasi bentuk bibir (*viseme*) atau isyarat tangan BISINDO/SIBI.
* **Hilangnya Data Belajar (No Auto-Draft):**
  Penyimpanan riwayat sesi murni manual via tombol klik. Jika tab tertutup tidak sengaja, seluruh rekaman percakapan dan kosakata hilang tanpa tersimpan ke `localStorage`.

---

### E. ATLAS (DevOps, Containerization & Production Operations)
* **User Privilege:** Container berjalan penuh sebagai `root` (UID 0), melanggar prinsip least privilege.
* **Ketiadaan `.dockerignore`:** Direktori `.git/`, `.env`, dan `firebase-credentials.json` berisiko ikut tersalin ke dalam Docker image (`COPY . .`).
* **Stateless Container Violation:**
  Foto profil pengguna disimpan ke direktori lokal container `static/uploads/profil`. Pada deployment container cloud (Cloud Run / K8s), disk bersifat ephemeral; foto profil akan **hilang setiap kali instance restart atau deploy baru**, serta menghasilkan error 404 pada arsitektur multi-replika.
* **Unpinned Dependencies:**
  `requirements.txt` tidak memiliki pinning versi, menyebabkan pip melakukan backtracking resolusi konflik antara `google-cloud-firestore`, `protobuf`, dan `google-generativeai`.
* **Ketiadaan Health Check & Structured Logging:**
  Tidak ada endpoint `/healthz` atau direktif `HEALTHCHECK` di Dockerfile untuk liveness/readiness probe load balancer. Logging masih menggunakan `print()` polos tanpa JSON format terstruktur.

---

### F. PRISM (QA Engineering, Testability & Data Integrity)
* **Test Coverage 0%:** Tidak ada test suite (unit, integration, maupun E2E). Tidak ada framework test (`pytest`) di `requirements.txt`.
* **Tight Coupling Eksternal:** Inisialisasi Firebase dan Google Cloud Translate dilakukan di level modul global, membuat import modul otomatis memicu koneksi network atau error jika kredensial tidak ada.
* **Integritas Relasi Rapuh:**
  Relasi kelas ke guru menggunakan string pencocokan nama (`guru_pengajar`), bukan foreign key / UUID. Typo huruf kapital atau nama kembar akan memutus relasi data kelas guru (seperti yang dicoba ditambal di `fix_db.py`).
* **Firestore Document ID Injection:**
  Pada `api_generate_kata_fokus`, ID dokumen dibuat dari string input: `f"{kata_dicari}_{lang}"`. Jika input mengandung karakter slash `/` (misal: "and/or"), Firestore melempar exception `InvalidArgument` karena menganggapnya sub-koleksi.
* **Inversi Logika Kuis AI vs Manual:**
  Kontrak data output soal kuis antara jalur AI Gemini dan fallback manual tidak konsisten (arah bahasa pertanyaan dan kunci jawaban terbalik).

---

## 3. Roadmap Tindakan Perbaikan (Prioritas Bertahap)

### Tahap 1: Hotfix Keamanan & Integritas (Urgent - P0)
1. **Perbaiki Alur Lupa Sandi:** Hapus fallback nomor bebas pada `app.py:320-325`. Akun tanpa nomor WA wajib verifikasi langsung ke Administrator.
2. **Kriptografi Password:** Terapkan hashing password menggunakan `werkzeug.security.generate_password_hash` (scrypt / pbkdf2) dan migrasikan data Firestore.
3. **Amankan Rute `/api/*`:** Pasang decorator autentikasi `@login_required` dan batasi akses API hanya untuk pengguna yang memiliki sesi aktif.
4. **Isolasi Secret Key Flask:** Wajibkan environment variable `SECRET_KEY` terisi saat startup dan buang fallback teks default.
5. **Amankan Kunci Deepgram:** Hentikan pemaparan master key ke template HTML. Terbitkan temporary ephemeral scoped key via backend API atau gunakan WebSocket relay internal.
6. **Cegah Stored XSS:** Ganti pemakaian `.innerHTML` pada render transkrip dengan `.textContent` atau bersihkan dengan DOMPurify.

### Tahap 2: Refactoring Arsitektur & Performa (High - P1)
1. **Pecah Monolith ke Blueprints & Services:**
   * Pisahkan `app.py` menjadi Blueprint: `auth`, `admin`, `kepsek`, `guru`, `api`.
   * Pisahkan logika bisnis ke `services/`: `ai_service.py`, `translate_service.py`, `stt_service.py`, `wa_service.py`.
   * Pindahkan kamus raksasa ke `constants/phonetics_dict.py`.
2. **Unifikasi Media ke Cloud Storage:** Pindahkan upload foto profil dari disk lokal container ke Firebase Storage (`storage.bucket()`), selaras dengan modul materi pembelajaran.
3. **Optimasi Sinkronisasi Realtime:**
   * Hentikan HTTP POST banjir per interim audio chunk.
   * Gunakan Firebase Auth token agar client berinteraksi langsung dengan Firebase RTDB per-room `kelas_id`, atau batasi rate sinkronisasi (trailing throttle 500ms).
4. **Optimasi Query Firestore:**
   * Buat composite index resmi untuk riwayat sesi (`nama_kelas` ASC + `timestamp` DESC).
   * Ganti relasi nama string dengan `guru_id` / NIP.
   * Gunakan query direct `.where('nip', '==', nip).limit(1)` daripada full stream scan.
5. **Pembaruan SDK Gemini AI:** Migrasikan library deprecated `google.generativeai` ke SDK resmi `google-genai` dan gunakan model stabil `gemini-2.0-flash` atau `gemini-1.5-flash` dengan format structured JSON native.

### Tahap 3: Frontend Modernization & Aksesibilitas SLB (Medium - P2)
1. **Modularitas Script Frontend:**
   * Pecah `stt_session.html` menjadi modul JavaScript terpisah: `audio-recorder.js`, `websocket-stt.js`, `caption-renderer.js`, `quiz-game.js`.
   * Pindahkan 504 baris CSS inline ke file `/static/css/stt_session.css`.
2. **Aksesibilitas & Mode Proyektor Kelas:**
   * Buat tombol "Mode Proyektor" (Fullscreen + Tema Gelap Kontras Tinggi `#090D16` dengan teks putih tajam dan aksen kuning).
   * Geser breakpoint responsive kolom dari 1024px ke 840px agar layout tetap berdampingan di proyektor XGA (1024x768).
   * Perbaiki kontras teks interim agar memenuhi standar WCAG AA (rasio minimal 4.5:1).
   * Eksplorasi penambahan ilustrasi visual artikulasi bibir (*viseme*) atau isyarat BISINDO pada panel kata fokus.
3. **Pangkas Jeda Suara:** Kurangi jeda hening `timerJedaKalimat` dari 5 detik ke 1.2 - 1.5 detik atau manfaatkan event VAD `SpeechFinished` Deepgram.
4. **Perbaiki Responsiveness Dashboard:**
   * Tambahkan media queries pada `dashboard.css`.
   * Ganti fixed sidebar 260px dengan mobile off-canvas drawer di layar <768px.
   * Hapus `overflow: hidden; height: 100vh;` dari body dashboard.
5. **Hapus Single-Tab Script:** Ganti validasi tab sessionStorage dengan HTTP-only session cookie standar di server.

### Tahap 4: DevOps & QA Hardening (Medium - P2)
1. **Container Hardening:**
   * Tambahkan file `.dockerignore`.
   * Update Dockerfile menggunakan base image `python:3.11-slim`, buat user non-root (`appuser`), dan dukung dynamic port injection (`${PORT:-8080}`).
   * Tambahkan endpoint `/healthz` dan direktif `HEALTHCHECK`.
2. **Dependency Pinning:** Buat file `requirements.lock` yang telah teruji kompatibilitasnya.
3. **Test Automation Suite:**
   * Bangun test suite dengan `pytest`.
   * Terapkan Application Factory (`create_app`) untuk memudahkan pengujian terisolasi.
   * Uji alur auth, parsing fonetik, dan kuis game menggunakan mock Firebase & Gemini.
   * Uji frontend browser & audio stream menggunakan Playwright dengan synthetic microphone capture.

---

## 4. Evidence Manifest V2

```text
EVIDENCE MANIFEST V2
- E1 | FACT | Password pengguna tersimpan dan dicocokkan dalam format plaintext di Firestore
  Reference: /root/projects/BridgeA/app.py:170, 195, 220, 260, 423, 452
- E2 | FACT | Fallback lupa sandi mengizinkan registrasi nomor baru pada akun tanpa no_wa (Account Takeover)
  Reference: /root/projects/BridgeA/app.py:320-325
- E3 | FACT | Master Deepgram API Key diekspos ke template HTML dan dipakai langsung di client-side WebSocket
  Reference: /root/projects/BridgeA/app.py:922-923; /root/projects/BridgeA/templates/stt_session.html:746, 832
- E4 | FACT | Middleware before_request hanya memvalidasi rute /admin, /kepsek, /guru; rute /api/* tidak terproteksi
  Reference: /root/projects/BridgeA/app.py:140-158
- E5 | FACT | Foto profil disimpan di filesystem lokal container sementara modul di-stream ke Firebase Storage
  Reference: /root/projects/BridgeA/app.py:42-44, 697-702, 994-998
- E6 | FACT | Dependency requirements.txt tanpa version pinning memicu backtracking resolver pip pada protobuf
  Reference: /root/projects/BridgeA/requirements.txt:1-7
- E7 | FACT | CSS dashboard memiliki 0 media query dan mengunci body dengan overflow:hidden
  Reference: /root/projects/BridgeA/static/css/dashboard.css:44-53, 96-104
- E8 | FACT | Jeda kalimat pada Sesi STT ditunda secara artifisial selama 5.000 ms sebelum memicu translate & AI
  Reference: /root/projects/BridgeA/templates/stt_session.html:904-907
- E9 | FACT | Tidak ditemukan file test suite otomatis pada repository BridgeA
  Reference: search_files pattern "*test*" mengembalikan 0 file pengujian
- E10 | OBSERVATION | Teks interim memiliki rasio kontras 2.5:1 (gagal WCAG AA 4.5:1)
  Reference: /root/projects/BridgeA/templates/stt_session.html:912, 914
```
