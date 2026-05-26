import os
import sys
import time
import random
import subprocess
import tempfile
import shutil
import requests
from flask import Flask, request, jsonify, render_template, send_file, Response
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

COOKIES_FILE = os.path.join(os.path.dirname(__file__), 'cookies.txt')

# ================================================================
# إعداد ffmpeg
# ================================================================
def setup_ffmpeg():
    if shutil.which('ffmpeg'):
        return shutil.which('ffmpeg'), True
    try:
        import imageio_ffmpeg
        src      = imageio_ffmpeg.get_ffmpeg_exe()
        is_win   = sys.platform == 'win32'
        exe_name = 'ffmpeg.exe' if is_win else 'ffmpeg'
        bin_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ffmpeg_bin')
        os.makedirs(bin_dir, exist_ok=True)
        dst = os.path.join(bin_dir, exe_name)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            if not is_win:
                os.chmod(dst, 0o755)
        return dst, True
    except Exception as e:
        print(f'[ffmpeg] {e}')
    return None, False

FFMPEG_EXE, FFMPEG_OK = setup_ffmpeg()
print(f'[startup] ffmpeg={FFMPEG_EXE} | ok={FFMPEG_OK}')

try:
    import curl_cffi
    CURL_CFFI_OK = True
except ImportError:
    CURL_CFFI_OK = False

# ================================================================
# User-Agents
# ================================================================
DESKTOP_UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
]
MOBILE_UAS = [
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36',
]

# ================================================================
# كشف نوع الموقع
# ================================================================
def is_tiktok(url):
    return any(d in url for d in ('tiktok.com', 'vt.tiktok', 'vm.tiktok'))

def is_instagram(url):
    return 'instagram.com' in url or 'instagr.am' in url

def is_twitter(url):
    return 'twitter.com' in url or 'x.com' in url

def is_youtube(url):
    return 'youtube.com' in url or 'youtu.be' in url

# ================================================================
# TikTok بدون كوكيز — طبقات متعددة
# ================================================================
def tiktok_via_tikwm(url):
    """
    tikwm.com — الأقوى: فيديو بدون علامة مائية، بدون كوكيز
    """
    try:
        r = requests.post(
            'https://www.tikwm.com/api/',
            data={'url': url, 'count': 12, 'cursor': 0, 'web': 1, 'hd': 1},
            headers={
                'User-Agent':   random.choice(DESKTOP_UAS),
                'Referer':      'https://www.tikwm.com/',
                'Origin':       'https://www.tikwm.com',
                'Accept':       'application/json, text/javascript, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            },
            timeout=30
        )
        if r.status_code == 200:
            data = r.json()
            if data.get('code') == 0:
                d = data.get('data', {})
                hd = d.get('hdplay') or d.get('play')
                sd = d.get('play') or d.get('hdplay')
                if hd:
                    return {
                        'hd':       hd,
                        'sd':       sd,
                        'title':    d.get('title', 'tiktok_video'),
                        'author':   d.get('author', {}).get('nickname', ''),
                        'cover':    d.get('cover', ''),
                        'duration': d.get('duration'),
                    }
    except Exception as e:
        print(f'[tikwm] {e}')
    return None

def tiktok_via_tikmate(url):
    """tikmate.app — fallback ثاني"""
    try:
        r = requests.get(
            f'https://api.tikmate.app/api/lookup?url={url}',
            headers={'User-Agent': random.choice(DESKTOP_UAS)},
            timeout=20
        )
        if r.status_code == 200:
            data = r.json()
            video_url = data.get('video_url') or data.get('video')
            if video_url and video_url.startswith('http'):
                return {
                    'hd':       video_url,
                    'sd':       video_url,
                    'title':    data.get('title', 'tiktok_video'),
                    'author':   '',
                    'cover':    data.get('thumbnail', ''),
                    'duration': None,
                }
    except Exception as e:
        print(f'[tikmate] {e}')
    return None

def tiktok_via_savetik(url):
    """savetik.net — fallback ثالث"""
    try:
        r = requests.post(
            'https://savetik.co/api/ajaxSearch',
            data={'q': url, 'lang': 'en'},
            headers={
                'User-Agent': random.choice(DESKTOP_UAS),
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': 'https://savetik.co/',
            },
            timeout=20
        )
        if r.status_code == 200:
            from html.parser import HTMLParser
            import re
            # نبحث عن أول رابط فيديو mp4
            links = re.findall(r'href=["\']?(https://[^"\'> ]+\.mp4[^"\'> ]*)', r.text)
            if links:
                return {
                    'hd': links[0], 'sd': links[-1],
                    'title': 'tiktok_video', 'author': '',
                    'cover': '', 'duration': None,
                }
    except Exception as e:
        print(f'[savetik] {e}')
    return None

def get_tiktok_info(url):
    """جرب كل الـ APIs بالترتيب حتى يجب واحدة تشتغل"""
    for name, fn in [('tikwm', tiktok_via_tikwm),
                     ('tikmate', tiktok_via_tikmate),
                     ('savetik', tiktok_via_savetik)]:
        result = fn(url)
        if result:
            print(f'[tiktok] {name} succeeded')
            return result
        print(f'[tiktok] {name} failed')
    return None

# ================================================================
# Instagram بدون كوكيز
# ================================================================
def instagram_via_instaloader(url):
    try:
        import instaloader
        loader = instaloader.Instaloader()
        if '/p/' in url:
            sc = url.split('/p/')[-1].split('/')[0]
        elif '/reel/' in url:
            sc = url.split('/reel/')[-1].split('/')[0]
        else:
            return None
        post = instaloader.Post.from_shortcode(loader.context, sc)
        if post.is_video:
            return {
                'hd': post.video_url, 'sd': post.video_url,
                'title': post.title or 'instagram_video',
                'author': post.owner_username, 'cover': post.url,
                'duration': None,
            }
    except Exception as e:
        print(f'[instaloader] {e}')
    return None

# ================================================================
# خيارات يوتيوب (ffmpeg للدمج)
# ================================================================
def get_youtube_opts(extra=None):
    opts = {
        'quiet':               True,
        'no_warnings':         True,
        'noplaylist':          True,
        'format':              'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
    }
    if FFMPEG_EXE:
        opts['ffmpeg_location'] = os.path.dirname(FFMPEG_EXE)
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 50:
        opts['cookiefile'] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts

# ================================================================
# خيارات المنصات الأخرى (مع impersonate)
# ================================================================
def get_other_opts(url='', extra=None):
    opts = {
        'quiet': True, 'no_warnings': True,
        'noplaylist': True, 'retries': 3,
        'nocheckcertificate': True,
    }
    if is_twitter(url):
        opts['http_headers'] = {'User-Agent': random.choice(DESKTOP_UAS)}
    else:
        if CURL_CFFI_OK:
            opts['impersonate'] = 'chrome'
        opts['http_headers'] = {
            'User-Agent': random.choice(MOBILE_UAS),
            'Accept-Language': 'en-US,en;q=0.9',
        }
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 50:
        opts['cookiefile'] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts

# ================================================================
# دمج بـ ffmpeg
# ================================================================
def merge_video_audio(video_path, audio_path, output_path):
    cmd = [FFMPEG_EXE, '-y', '-i', video_path, '-i', audio_path,
           '-c:v', 'copy', '-c:a', 'aac', '-strict', 'experimental', output_path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        print(f'[ffmpeg error]\n{r.stderr[-500:]}')
        return False
    return True

# ================================================================
# تنزيل مباشر وإرسال للمستخدم
# ================================================================
def stream_from_url(video_url, filename='video.mp4'):
    """تنزيل الفيديو من URL وإرساله مباشرة للمستخدم"""
    r = requests.get(
        video_url,
        headers={
            'User-Agent': random.choice(MOBILE_UAS),
            'Referer': 'https://www.tiktok.com/',
        },
        stream=True,
        timeout=120
    )
    r.raise_for_status()
    tmpdir = tempfile.mkdtemp()
    out    = os.path.join(tmpdir, filename)
    with open(out, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return out, tmpdir

# ================================================================
# رسائل خطأ
# ================================================================
def friendly_error(e, url=''):
    err = str(e)
    if '429' in err or 'Too Many Requests' in err:
        return '⏳ الموقع طلب الانتظار. جرّب بعد دقيقة.'
    if '403' in err or 'Forbidden' in err:
        return '🔒 الموقع رفض الوصول. جرّب رابطاً آخر.'
    if 'Private' in err or 'private' in err:
        return '🔒 المحتوى خاص. لا يمكن تنزيله.'
    if 'Login' in err or 'login' in err:
        return '🔑 هذا المحتوى يتطلب تسجيل دخول.'
    return err

# ================================================================
# Routes
# ================================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok', 'ffmpeg': FFMPEG_OK})

@app.route('/debug')
def debug():
    return jsonify({'ffmpeg_ok': FFMPEG_OK, 'curl_cffi': CURL_CFFI_OK,
                    'ffmpeg_path': FFMPEG_EXE, 'platform': sys.platform})

# ─────────────────────────────────────────────────────────────────
# /info — جلب المعلومات والـ formats
# ─────────────────────────────────────────────────────────────────
@app.route('/info', methods=['GET'])
def get_info():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    # ── يوتيوب ────────────────────────────────────────────────────
    if is_youtube(url):
        try:
            with yt_dlp.YoutubeDL(get_youtube_opts({'skip_download': True})) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            return jsonify({'error': friendly_error(e, url)}), 500
        return _format_info(info, url)

    # ── TikTok: APIs خارجية أولاً (بدون كوكيز) ───────────────────
    if is_tiktok(url):
        data = get_tiktok_info(url)
        if data:
            formats = []
            if data.get('hd'):
                formats.append({
                    'format_id': 'api_hd',
                    'ext':       'mp4',
                    'height':    1080,
                    'fps':       30,
                    'vcodec':    'h264',
                    'acodec':    'aac',
                    'filesize':  None,
                    'abr':       128,
                    'has_audio': True,
                    'has_video': True,
                })
            if data.get('sd') and data['sd'] != data.get('hd'):
                formats.append({
                    'format_id': 'api_sd',
                    'ext':       'mp4',
                    'height':    720,
                    'fps':       30,
                    'vcodec':    'h264',
                    'acodec':    'aac',
                    'filesize':  None,
                    'abr':       128,
                    'has_audio': True,
                    'has_video': True,
                })
            return jsonify({
                'title':        data.get('title', 'TikTok Video'),
                'uploader':     data.get('author', ''),
                'duration':     data.get('duration'),
                'thumbnail':    data.get('cover', ''),
                'webpage_url':  url,
                'formats':      formats,
                'ffmpeg_ok':    FFMPEG_OK,
                'is_tiktok':    True,
                'is_instagram': False,
                'is_twitter':   False,
                'is_youtube':   False,
            })
        # لو كل الـ APIs فشلت جرب yt-dlp كـ last resort
        try:
            with yt_dlp.YoutubeDL(get_other_opts(url, {'skip_download': True})) as ydl:
                info = ydl.extract_info(url, download=False)
            return _format_info(info, url)
        except Exception as e:
            return jsonify({'error': '❌ تعذّر الوصول لهذا الفيديو. قد يكون خاصاً أو محذوفاً.'}), 500

    # ── Instagram ─────────────────────────────────────────────────
    if is_instagram(url):
        # جرب instaloader أولاً
        data = instagram_via_instaloader(url)
        if data:
            return jsonify({
                'title':        data.get('title', 'Instagram Video'),
                'uploader':     data.get('author', ''),
                'duration':     data.get('duration'),
                'thumbnail':    data.get('cover', ''),
                'webpage_url':  url,
                'formats': [{
                    'format_id': 'api_hd',
                    'ext':       'mp4',
                    'height':    1080,
                    'fps':       30,
                    'vcodec':    'h264',
                    'acodec':    'aac',
                    'filesize':  None,
                    'abr':       128,
                    'has_audio': True,
                    'has_video': True,
                }],
                'ffmpeg_ok':    FFMPEG_OK,
                'is_tiktok':    False,
                'is_instagram': True,
                'is_twitter':   False,
                'is_youtube':   False,
            })
        # fallback لـ yt-dlp مع impersonate
        try:
            with yt_dlp.YoutubeDL(get_other_opts(url, {'skip_download': True})) as ydl:
                info = ydl.extract_info(url, download=False)
            return _format_info(info, url)
        except Exception as e:
            return jsonify({'error': friendly_error(e, url)}), 500

    # ── باقي المواقع: yt-dlp مباشرة ──────────────────────────────
    try:
        with yt_dlp.YoutubeDL(get_other_opts(url, {'skip_download': True})) as ydl:
            info = ydl.extract_info(url, download=False)
        return _format_info(info, url)
    except Exception as e:
        return jsonify({'error': friendly_error(e, url)}), 500

def _format_info(info, url):
    formats = []
    for f in info.get('formats', []):
        if not f.get('url'):
            continue
        has_v = f.get('vcodec') not in (None, 'none')
        has_a = f.get('acodec') not in (None, 'none')
        formats.append({
            'format_id': f.get('format_id'),
            'ext':       f.get('ext'),
            'height':    f.get('height'),
            'fps':       f.get('fps'),
            'vcodec':    f.get('vcodec'),
            'acodec':    f.get('acodec'),
            'filesize':  f.get('filesize'),
            'abr':       f.get('abr'),
            'has_audio': has_a,
            'has_video': has_v,
        })
    formats.sort(key=lambda x: (x.get('height') or 0), reverse=True)
    return jsonify({
        'title':        info.get('title'),
        'uploader':     info.get('uploader'),
        'duration':     info.get('duration'),
        'thumbnail':    info.get('thumbnail'),
        'webpage_url':  info.get('webpage_url', url),
        'formats':      formats,
        'ffmpeg_ok':    FFMPEG_OK,
        'is_youtube':   is_youtube(url),
        'is_tiktok':    is_tiktok(url),
        'is_instagram': is_instagram(url),
        'is_twitter':   is_twitter(url),
    })

# ─────────────────────────────────────────────────────────────────
# /download — تنزيل الفيديو
# ─────────────────────────────────────────────────────────────────
@app.route('/download', methods=['GET'])
def download():
    url       = request.args.get('url', '').strip()
    format_id = request.args.get('format_id', '').strip()
    has_audio = request.args.get('has_audio', 'false').strip().lower() == 'true'

    if not url:
        return jsonify({'error': 'No URL'}), 400

    tmpdir = tempfile.mkdtemp()
    try:
        safe_title = 'video'

        # ══ format_id يبدأ بـ api_ → استخدم TikTok/Instagram API ══
        if format_id.startswith('api_'):
            quality = 'hd' if format_id == 'api_hd' else 'sd'

            if is_tiktok(url):
                data = get_tiktok_info(url)
                if not data:
                    return jsonify({'error': '❌ فشل التنزيل من TikTok. جرّب لاحقاً.'}), 500
                video_url  = data.get(quality) or data.get('hd') or data.get('sd')
                safe_title = _safe(data.get('title', 'tiktok_video'))

            elif is_instagram(url):
                data = instagram_via_instaloader(url)
                if not data:
                    return jsonify({'error': '❌ فشل التنزيل من Instagram.'}), 500
                video_url  = data.get('hd')
                safe_title = _safe(data.get('title', 'instagram_video'))
            else:
                return jsonify({'error': 'غير مدعوم'}), 400

            out, td2 = stream_from_url(video_url, f'{safe_title}.mp4')
            return send_file(out, as_attachment=True,
                             download_name=f'{safe_title}.mp4')

        # ══ يوتيوب: ffmpeg للدمج ══════════════════════════════════
        if is_youtube(url):
            if has_audio:
                fmt     = format_id if format_id else 'best[ext=mp4]/best'
                outtmpl = os.path.join(tmpdir, 'output.%(ext)s')
                with yt_dlp.YoutubeDL(get_youtube_opts({'format': fmt, 'outtmpl': outtmpl})) as ydl:
                    info = ydl.extract_info(url, download=True)
                safe_title = _safe(info.get('title', 'video'))
                files = [f for f in os.listdir(tmpdir) if not f.endswith(('.part', '.ytdl'))]
                if not files:
                    return jsonify({'error': 'فشل التحميل'}), 500
                return send_file(os.path.join(tmpdir, files[0]),
                                 as_attachment=True, download_name=f'{safe_title}.mp4')
            else:
                if not FFMPEG_OK:
                    return jsonify({'error': '⚠ ffmpeg غير متاح'}), 500
                vfmt  = format_id if format_id else 'bestvideo[ext=mp4]/bestvideo'
                vtmpl = os.path.join(tmpdir, 'video.%(ext)s')
                with yt_dlp.YoutubeDL(get_youtube_opts({'format': vfmt, 'outtmpl': vtmpl})) as ydl:
                    info = ydl.extract_info(url, download=True)
                safe_title = _safe(info.get('title', 'video'))
                vfiles = [f for f in os.listdir(tmpdir) if f.startswith('video.')]
                if not vfiles:
                    return jsonify({'error': 'فشل تنزيل الفيديو'}), 500

                atmpl = os.path.join(tmpdir, 'audio.%(ext)s')
                with yt_dlp.YoutubeDL(get_youtube_opts({'format': 'bestaudio', 'outtmpl': atmpl})) as ydl:
                    ydl.extract_info(url, download=True)
                afiles = [f for f in os.listdir(tmpdir) if f.startswith('audio.')]
                if not afiles:
                    return jsonify({'error': 'فشل تنزيل الصوت'}), 500

                out = os.path.join(tmpdir, f'{safe_title}.mp4')
                if not merge_video_audio(os.path.join(tmpdir, vfiles[0]),
                                         os.path.join(tmpdir, afiles[0]), out):
                    return jsonify({'error': 'فشل الدمج'}), 500
                return send_file(out, as_attachment=True, download_name=f'{safe_title}.mp4')

        # ══ المنصات الأخرى: yt-dlp مع impersonate ════════════════
        fmt     = format_id if format_id else ('best[ext=mp4]/best' if has_audio else 'bestvideo[ext=mp4]/bestvideo')
        outtmpl = os.path.join(tmpdir, 'output.%(ext)s')
        opts    = get_other_opts(url, {'format': fmt, 'outtmpl': outtmpl})
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        safe_title = _safe(info.get('title', 'video'))
        files = [f for f in os.listdir(tmpdir) if not f.endswith(('.part', '.ytdl'))]
        if not files:
            return jsonify({'error': 'فشل التحميل'}), 500
        return send_file(os.path.join(tmpdir, files[0]),
                         as_attachment=True, download_name=f'{safe_title}.mp4')

    except Exception as e:
        return jsonify({'error': friendly_error(e, url)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def _safe(s):
    return ''.join(c for c in str(s) if c.isalnum() or c in ' _-')[:80].strip() or 'video'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
