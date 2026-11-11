import http.server
import urllib.parse
import json
import os
import io
import zipfile
import html
import sys
import time
import re
import traceback
import requests
from PIL import Image, ImageOps, ImageEnhance, ImageFilter, ImageDraw

# ==============================================================================
# Xteink X4 Manga OPDS Cloud Bridge - v7.0 Ultimate (Google Drive + MangaDex)
# - Nguồn 1: Google Drive cá nhân (Thư mục do bạn quản lý, 100% không DMCA, không Cloudflare)
# - Nguồn 2: MangaDex Open API (Kho truyện quốc tế phong phú)
# - Tối ưu e-ink 480x800: Tự cắt viền trắng, chia đôi trang ngang, cắt dải manhwa
# - Phân đoạn chương 40 tập/nhóm, hỗ trợ tìm nhanh số chương
# - Bộ đệm Cache RAM + Đĩa cứng giúp tải siêu tốc
# ==============================================================================

# ID Thư mục Google Drive của bạn
GDRIVE_ROOT_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID', '1nVTOBrtQQK92TgiEyzKKdLx8UsiQQb2-')

MANGADEX_API = "https://api.mangadex.org"
MANGADEX_COVER_BASE = "https://uploads.mangadex.org/covers"
HEADERS = {
    "User-Agent": "XteinkMangaOPDS/7.0 (Xteink X4 E-Reader Bridge; https://github.com/hieunguyen1666/xteink-manga)",
    "Accept": "application/json"
}

# RAM Cache (TTL 10 phút)
RAM_CACHE = {}

# Disk Cache cho các tệp EPUB đã tối ưu
DISK_CACHE_DIR = os.environ.get('CACHE_DIR', '/tmp/xteink_manga_cache' if os.name != 'nt' else 'C:\\Users\\LENOVO\\manga_server\\cache')
os.makedirs(DISK_CACHE_DIR, exist_ok=True)

def xml_escape(text):
    return html.escape(str(text or ''))

def parse_num(val):
    try:
        m = re.search(r'(\d+(\.\d+)?)', str(val))
        return float(m.group(1)) if m else -1.0
    except Exception:
        return -1.0

# ------------------------------------------------------------------------------
# 1. Thuật toán tối ưu hóa ảnh cho màn hình E-Ink 480x800
# ------------------------------------------------------------------------------

def crop_margins(im, threshold=242, margin_pad=4):
    """Cắt sạch viền trắng thừa để chữ và hình vẽ phóng to tối đa trên E-Ink."""
    if im.mode != 'L':
        im = ImageOps.grayscale(im)
    w, h = im.size
    inv = ImageOps.invert(im)
    mask = inv.point(lambda p: 255 if p > (255 - threshold) else 0)
    bbox = mask.getbbox()
    if bbox:
        left, upper, right, lower = bbox
        left = max(0, left - margin_pad)
        upper = max(0, upper - margin_pad)
        right = min(w, right + margin_pad)
        lower = min(h, lower + margin_pad)
        if (right - left) > w * 0.60 and (lower - upper) > h * 0.60:
            return im.crop((left, upper, right, lower))
    return im

def optimize_and_split_image(img_bytes):
    """
    Xử lý thông minh hình ảnh:
    - Cắt viền trắng
    - Trang đôi ngang -> Tách thành 2 trang dọc (Phải -> Trái) phóng to 200%
    - Dải cuộn dọc Manhwa/Webtoon -> Cắt thành các trang vừa khít tỉ lệ 480x800
    - Tăng tương phản và độ nét (Sharpen) cho chữ đen rõ nét trên màn hình E-Ink
    """
    im = Image.open(io.BytesIO(img_bytes))
    if im.mode != 'L':
        im = ImageOps.grayscale(im)

    im = crop_margins(im)
    w, h = im.size
    sub_images = []

    if w > h * 1.25:
        mid = w // 2
        right_page = crop_margins(im.crop((mid, 0, w, h)))
        left_page = crop_margins(im.crop((0, 0, mid, h)))
        sub_images.extend([right_page, left_page])
    elif h > w * 1.55:
        target_h = int(w * 1.58)
        overlap = int(w * 0.12)
        curr_y = 0
        while curr_y < h:
            chunk_h = min(target_h, h - curr_y)
            cropped = im.crop((0, curr_y, w, min(curr_y + chunk_h, h)))
            sub_images.append(crop_margins(cropped))
            if curr_y + chunk_h >= h:
                break
            curr_y += (chunk_h - overlap)
    else:
        sub_images.append(im)

    final_buffers = []
    for sub in sub_images:
        sub.thumbnail((480, 800), Image.Resampling.LANCZOS)
        sub = ImageOps.autocontrast(sub, cutoff=1)
        sub = sub.filter(ImageFilter.UnsharpMask(radius=1.0, percent=125, threshold=2))

        buf = io.BytesIO()
        sub.save(buf, format='JPEG', quality=88, optimize=True, subsampling=0)
        final_buffers.append(buf.getvalue())

    return final_buffers

def build_epub_file(book_id, book_title, processed_images):
    """Đóng gói mảng ảnh thành tệp EPUB chuẩn đọc mượt mà trên CrossPoint Reader."""
    esc_safe_title = html.escape(book_title)
    epub_buf = io.BytesIO()
    with zipfile.ZipFile(epub_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

        zf.writestr("META-INF/container.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>''')

        manifest = ['<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
        spine = []

        for idx, img_bytes in enumerate(processed_images):
            num = idx + 1
            img_name = f"page_{num:03d}.jpg"
            xhtml_name = f"page_{num:03d}.xhtml"
            zf.writestr(f"OEBPS/images/{img_name}", img_bytes)

            page_xhtml = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>Page {num}</title>
    <style type="text/css">
        @page {{ margin: 0; padding: 0; }}
        body {{ margin: 0; padding: 0; background-color: #ffffff; text-align: center; }}
        img {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
    </style>
</head>
<body>
    <img src="images/{img_name}" alt="Page {num}"/>
</body>
</html>'''
            zf.writestr(f"OEBPS/{xhtml_name}", page_xhtml)
            manifest.append(f'<item id="img_{num:03d}" href="images/{img_name}" media-type="image/jpeg"/>')
            manifest.append(f'<item id="page_{num:03d}" href="{xhtml_name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="page_{num:03d}"/>')

        content_opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>{esc_safe_title}</dc:title>
        <dc:language>vi</dc:language>
        <dc:identifier id="BookID">urn:manga:{book_id}</dc:identifier>
        <dc:creator>Xteink Manga Bridge</dc:creator>
    </metadata>
    <manifest>
        {''.join(manifest)}
    </manifest>
    <spine toc="ncx">
        {''.join(spine)}
    </spine>
</package>'''
        zf.writestr("OEBPS/content.opf", content_opf)

        toc_ncx = f'''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
    <head>
        <meta name="dtb:uid" content="urn:manga:{book_id}"/>
        <meta name="dtb:depth" content="1"/>
        <meta name="dtb:totalPageCount" content="0"/>
        <meta name="dtb:maxPageNumber" content="0"/>
    </head>
    <docTitle><text>{esc_safe_title}</text></docTitle>
    <navMap>
        <navPoint id="navPoint-1" playOrder="1">
            <navLabel><text>{esc_safe_title}</text></navLabel>
            <content src="page_001.xhtml"/>
        </navPoint>
    </navMap>
</ncx>'''
        zf.writestr("OEBPS/toc.ncx", toc_ncx)

    return epub_buf.getvalue()

def generate_dmca_notice_epub(manga_title):
    """Tạo tệp EPUB thông báo rõ ràng khi bộ truyện bị gỡ bản quyền."""
    safe_title = f"Thong_bao_{manga_title[:20]}"
    esc_title = html.escape(manga_title)

    img = Image.new('L', (480, 800), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([15, 60, 465, 740], outline=0, width=4)
    draw.text((35, 100), "THONG BAO BAN QUYEN (DMCA)", fill=0)
    draw.line([35, 130, 445, 130], fill=0, width=2)
    draw.text((35, 160), "Bo truyen:", fill=0)
    draw.text((35, 195), f"{manga_title[:36]}", fill=0)
    draw.text((35, 255), "Cac chuong tieng Viet tren kho", fill=0)
    draw.text((35, 290), "MangaDex da bi go boi yeu cau", fill=0)
    draw.text((35, 325), "ban quyen tu nha xuat ban (DMCA).", fill=0)
    draw.line([35, 375, 445, 375], fill=0, width=1)
    draw.text((35, 405), "GIAI PHAP CHO BAN:", fill=0)
    draw.text((35, 440), "1. Mo muc [Kho Google Drive Cua Toi]", fill=0)
    draw.text((35, 475), "   ngay tai Trang Chu de tai ve doc!", fill=0)
    draw.text((35, 510), "2. Ban co the dung tool download_manga", fill=0)
    draw.text((35, 545), "   tren PC de tai them cac bo truyen khac.", fill=0)

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    epub_bytes = build_epub_file("dmca_notice", esc_title, [buf.getvalue()])
    return safe_title, epub_bytes

# ------------------------------------------------------------------------------
# 2. Google Drive Scraper & Direct Download Streamer
# ------------------------------------------------------------------------------

def fetch_gdrive_folder_items(folder_id):
    """
    Quét danh sách thư mục và file trong thư mục Google Drive công khai.
    Sử dụng endpoint embeddedfolderview của Google (không cần API key hay đăng nhập).
    """
    cache_key = f"gdrive_{folder_id}"
    now = time.time()
    if cache_key in RAM_CACHE:
        ts, data = RAM_CACHE[cache_key]
        if now - ts < 180:  # Cache 3 phút để cập nhật nhanh khi bạn tải truyện mới lên
            return data

    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    folders = []
    files = []
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            html_text = res.text

            # 1. Quét theo format entry của Google Drive embedded view
            # Link file dạng: https://drive.google.com/file/d/([-\w]{25,})/view
            file_entries = re.findall(r'/file/d/([-\w]{25,})/view[^"]*["\'][^>]*>([\s\S]*?)</a>', html_text)
            for fid, ftitle in file_entries:
                clean_name = re.sub(r'<[^>]+>', '', ftitle).strip()
                if clean_name and not any(f['id'] == fid for f in files):
                    files.append({'id': fid, 'name': clean_name})

            # 2. Quét subfolder: https://drive.google.com/drive/folders/([-\w]{25,})
            folder_entries = re.findall(r'/drive/folders/([-\w]{25,})[^"]*["\'][^>]*>([\s\S]*?)</a>', html_text)
            for fld_id, fld_title in folder_entries:
                if fld_id != folder_id:
                    clean_title = re.sub(r'<[^>]+>', '', fld_title).strip()
                    if clean_title and not any(f['id'] == fld_id for f in folders):
                        folders.append({'id': fld_id, 'title': clean_title})

            # 3. Fallback thêm nếu dùng flip-entry-title
            if not files and not folders:
                titles = re.findall(r'class="flip-entry-title">([^<]+)</div>', html_text)
                for t in titles:
                    files.append({'id': '', 'name': t.strip()})

    except Exception as e:
        print(f"[!] Lỗi đọc Google Drive folder {folder_id}: {e}")

    result = {'folders': folders, 'files': files}
    RAM_CACHE[cache_key] = (now, result)
    return result

def download_gdrive_file(file_id):
    """Tải trực tiếp nội dung file EPUB từ Google Drive (tự động xử lý cảnh báo dung lượng)."""
    session = requests.Session()
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    res = session.get(url, headers=headers, stream=True, timeout=30)
    # Xử lý cookie cảnh báo virus nếu file lớn
    for k, v in res.cookies.items():
        if k.startswith('download_warning'):
            url = f"https://drive.google.com/uc?export=download&confirm={v}&id={file_id}"
            res = session.get(url, headers=headers, stream=True, timeout=30)
            break

    return res.content

# ------------------------------------------------------------------------------
# 3. MangaDex API Integration (Global Manga)
# ------------------------------------------------------------------------------

def get_mangadex_title(attrs):
    titles = attrs.get('title', {})
    if 'vi' in titles:
        return titles['vi']
    for alt in attrs.get('altTitles', []):
        if 'vi' in alt:
            return alt['vi']
    if 'en' in titles:
        return titles['en']
    for v in titles.values():
        return v
    return "Manga"

def get_mangadex_cover(relationships):
    for rel in relationships:
        if rel.get('type') == 'cover_art':
            return rel.get('attributes', {}).get('fileName')
    return None

def fetch_mangadex_list(list_type, query=None, limit=30, offset=0):
    params = {
        "limit": limit,
        "offset": offset,
        "availableTranslatedLanguage[]": ["vi"],
        "includes[]": ["cover_art"],
        "contentRating[]": ["safe", "suggestive", "erotica"]
    }
    if query:
        params["title"] = query
    elif list_type == "popular":
        params["order[followedCount]"] = "desc"
    elif list_type == "rating":
        params["order[rating]"] = "desc"
    elif list_type == "latest":
        params["order[latestUploadedChapter]"] = "desc"
    elif list_type == "completed":
        params["status[]"] = "completed"
        params["order[followedCount]"] = "desc"
    else:
        params["order[followedCount]"] = "desc"

    res = requests.get(f"{MANGADEX_API}/manga", headers=HEADERS, params=params, timeout=15)
    res.raise_for_status()
    return res.json().get('data', [])

def fetch_mangadex_detail(manga_id):
    res = requests.get(f"{MANGADEX_API}/manga/{manga_id}", headers=HEADERS, timeout=15)
    res.raise_for_status()
    item = res.json().get('data', {})
    return get_mangadex_title(item.get('attributes', {}))

def fetch_mangadex_chapters(manga_id):
    now = time.time()
    cache_key = f"md_ch_{manga_id}"
    if cache_key in RAM_CACHE:
        ts, data = RAM_CACHE[cache_key]
        if now - ts < 900:
            return data

    params = {
        "limit": 500,
        "translatedLanguage[]": ["vi"],
        "order[chapter]": "desc",
        "includeExternalUrl": 0,
        "includeEmptyPages": 0
    }
    res = requests.get(f"{MANGADEX_API}/manga/{manga_id}/feed", headers=HEADERS, params=params, timeout=15)
    res.raise_for_status()
    chapters = res.json().get('data', [])
    RAM_CACHE[cache_key] = (now, chapters)
    return chapters

def generate_mangadex_epub(chapter_id, chapter_title):
    disk_file = os.path.join(DISK_CACHE_DIR, f"md_{chapter_id}.epub")
    if os.path.exists(disk_file) and os.path.getsize(disk_file) > 1024:
        print(f"[*] Phục vụ từ Cache đĩa cứng: {disk_file}")
        with open(disk_file, 'rb') as f:
            return chapter_title, f.read()

    res = requests.get(f"{MANGADEX_API}/at-home/server/{chapter_id}", headers=HEADERS, timeout=15)
    res.raise_for_status()
    srv_data = res.json()

    base_url = srv_data.get('baseUrl')
    chapter_info = srv_data.get('chapter', {})
    ch_hash = chapter_info.get('hash')
    file_list = chapter_info.get('dataSaver') or chapter_info.get('data') or []
    sub_folder = "data-saver" if chapter_info.get('dataSaver') else "data"

    if not file_list:
        raise ValueError("Không tìm thấy hình ảnh nào trong chương này trên MangaDex.")

    processed_images = []
    print(f"[*] [MangaDex] Đang tải và tối ưu {len(file_list)} trang ảnh cho: {chapter_title}...")
    for idx, fname in enumerate(file_list):
        img_url = f"{base_url}/{sub_folder}/{ch_hash}/{fname}"
        try:
            r = requests.get(img_url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                buffers = optimize_and_split_image(r.content)
                processed_images.extend(buffers)
        except Exception as e:
            print(f"[!] Lỗi tải trang {idx+1}: {e}")

    if not processed_images:
        raise ValueError("Không thể tải được hình ảnh hợp lệ nào từ chương này.")

    safe_title = chapter_title or f"Chapter_{chapter_id}"
    epub_bytes = build_epub_file(chapter_id, safe_title, processed_images)

    try:
        with open(disk_file, 'wb') as f:
            f.write(epub_bytes)
    except Exception:
        pass

    return safe_title, epub_bytes

# ------------------------------------------------------------------------------
# 4. Xây Dựng OPDS Feeds
# ------------------------------------------------------------------------------

def format_feed(base_url, feed_id, feed_title, entries):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:{feed_id}</id>
  <title>{xml_escape(feed_title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

def build_root_feed(base_url):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:root</id>
  <title>Kho Truyện Tranh (Xteink X4 E-Ink)</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <author><name>Xteink Manga Cloud Bridge v7.0</name></author>
  <link rel="self" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>

  <entry>
    <title>🌟 [KHO GOOGLE DRIVE CỦA TÔI]</title>
    <id>urn:xteink:gdrive:root</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Thư viện truyện cá nhân lưu trên Google Drive: Tôi Thăng Cấp Một Mình, Manhwa hot...</summary>
    <link rel="subsection" href="{base_url}/opds/gdrive" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🔍 Tìm Kiếm Nhanh (Gõ Tên / Gợi Ý Hot)</title>
    <id>urn:xteink:search_menu</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tìm kiếm nhanh các bộ truyện tranh yêu thích</summary>
    <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>
    <link rel="subsection" href="{base_url}/opds/search_shortcuts" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  </entry>

  <entry>
    <title>🔥 [MangaDex] Truyện Tiếng Việt Xem Nhiều Nhất</title>
    <id>urn:xteink:mangadex:popular</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Kho truyện tranh tiếng Việt được theo dõi nhiều nhất trên MangaDex</summary>
    <link rel="subsection" href="{base_url}/opds/list?type=popular" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>⭐ [MangaDex] Truyện Đánh Giá Cao Nhất</title>
    <id>urn:xteink:mangadex:rating</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Các tác phẩm truyện tranh có điểm số đánh giá cao nhất</summary>
    <link rel="subsection" href="{base_url}/opds/list?type=rating" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🆕 [MangaDex] Mới Cập Nhật Chương Mới</title>
    <id>urn:xteink:mangadex:latest</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Các bộ truyện tranh vừa được dịch và ra chap mới</summary>
    <link rel="subsection" href="{base_url}/opds/list?type=latest" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🏆 [MangaDex] Tuyển Chọn Trọn Bộ (Đã Hoàn Thành)</title>
    <id>urn:xteink:mangadex:completed</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Các bộ truyện tranh kinh điển đã hoàn thành (full chap)</summary>
    <link rel="subsection" href="{base_url}/opds/list?type=completed" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>
</feed>'''

# --- Google Drive Feeds ---

def build_gdrive_folder_feed(base_url, folder_id, folder_title):
    data = fetch_gdrive_folder_items(folder_id)
    folders = data.get('folders', [])
    files = data.get('files', [])

    entries = []

    # 1. Các thư mục con (Mỗi thư mục con là 1 bộ truyện tranh)
    for fld in folders:
        fld_id = fld['id']
        fld_title = fld['title']
        safe_title = urllib.parse.quote(fld_title)
        manga_url = f"{base_url}/opds/gdrive/manga?folder_id={fld_id}&amp;title={safe_title}"

        entries.append(f'''  <entry>
    <title>📁 {xml_escape(fld_title)}</title>
    <id>urn:gdrive:folder:{fld_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Mở danh sách các chương trong bộ truyện này</summary>
    <link rel="subsection" href="{manga_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    # 2. Các file EPUB nằm trực tiếp trong thư mục
    epub_files = [f for f in files if f['name'].lower().endswith(('.epub', '.cbz', '.zip')) or f['id']]
    for f in epub_files:
        fid = f['id']
        fname = f['name']
        display_name = fname.replace('.epub', '').replace('.cbz', '')
        safe_title = urllib.parse.quote(display_name)
        download_url = f"{base_url}/download_gdrive?file_id={fid}&amp;title={safe_title}"

        entries.append(f'''  <entry>
    <title>📖 {xml_escape(display_name)}</title>
    <id>urn:gdrive:file:{fid}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải file EPUB từ Google Drive về máy đọc sách</summary>
    <link rel="http://opds-spec.org/acquisition" href="{download_url}" type="application/epub+zip"/>
  </entry>''')

    if not entries:
        entries.append(f'''  <entry>
    <title>📁 Thư mục Google Drive hiện đang trống</title>
    <id>urn:gdrive:empty:{folder_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Hãy chạy tool 'download_manga.py' trên máy tính để tải truyện và kéo thả các file .epub vào thư mục Google Drive của bạn!</summary>
  </entry>''')

    return format_feed(base_url, f"gdrive:{folder_id}", folder_title, entries)

# --- MangaDex Feeds ---

def build_search_shortcuts_feed(base_url):
    keywords = [
        "One Piece", "Conan", "Doraemon", "Dragon Ball", "Naruto",
        "Jujutsu Kaisen", "Chainsaw Man", "Kimetsu no Yaiba", "Attack on Titan",
        "Bleach", "Hunter x Hunter", "Fairy Tail", "Spy x Family", "Berserk",
        "Tokyo Ghoul", "Black Clover", "Haikyuu", "Slam Dunk", "Death Note",
        "Frieren", "Oshi no Ko", "Kingdom", "Gintama", "Blue Lock"
    ]
    entries = []
    for kw in keywords:
        q_enc = urllib.parse.quote(kw)
        entries.append(f'''  <entry>
    <title>📖 {kw}</title>
    <id>urn:xteink:search:{q_enc}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Xem các tập của bộ "{kw}"</summary>
    <link rel="subsection" href="{base_url}/opds/search?q={q_enc}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:search:shortcuts</id>
  <title>Gợi Ý Tìm Kiếm Nhanh</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

def build_manga_list_feed(base_url, feed_title, list_type, manga_list):
    entries = []
    for item in manga_list:
        m_id = item['id']
        attrs = item.get('attributes', {})
        raw_title = get_mangadex_title(attrs)
        title = xml_escape(raw_title)

        desc_dict = attrs.get('description', {})
        desc_text = desc_dict.get('vi') or desc_dict.get('en') or 'Truyện tranh tiếng Việt'
        desc_clean = xml_escape(desc_text[:200] + ('...' if len(desc_text) > 200 else ''))

        cover_file = get_mangadex_cover(item.get('relationships', []))
        cover_link_xml = ""
        if cover_file:
            cover_url = f"{MANGADEX_COVER_BASE}/{m_id}/{cover_file}.256.jpg"
            cover_link_xml = f'''<link rel="http://opds-spec.org/image" href="{cover_url}" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{cover_url}" type="image/jpeg"/>'''

        safe_manga_title = urllib.parse.quote(raw_title)
        entries.append(f'''  <entry>
    <title>{title}</title>
    <id>urn:mangadex:manga:{m_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{desc_clean}</summary>
    {cover_link_xml}
    <link rel="subsection" href="{base_url}/opds/manga?id={m_id}&amp;title={safe_manga_title}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:mangadex:list:{list_type}</id>
  <title>{xml_escape(feed_title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

def create_chapter_entry(base_url, manga_title, ch):
    ch_id = ch['id']
    attrs = ch.get('attributes', {})
    ch_num = attrs.get('chapter') or 'OneShot'
    ch_title = attrs.get('title') or ''
    full_title = f"Chương {ch_num}" + (f": {ch_title}" if ch_title else "")
    esc_title = xml_escape(full_title)

    pages_count = attrs.get('pages', 0)
    summary = f"{pages_count} trang ảnh - Tiếng Việt" if pages_count else "Tiếng Việt"

    safe_ch_title = urllib.parse.quote(f"{manga_title} - Chap {ch_num}")
    download_url = f"{base_url}/download?chapter_id={ch_id}&amp;title={safe_ch_title}"

    return f'''  <entry>
    <title>{esc_title}</title>
    <id>urn:mangadex:chapter:{ch_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{summary}</summary>
    <link rel="http://opds-spec.org/acquisition" href="{download_url}" type="application/epub+zip"/>
  </entry>'''

def build_chapter_feed(base_url, manga_id, manga_title, all_chapters, order='desc', range_param=None, search_q=None):
    entries = []
    safe_manga_title = urllib.parse.quote(manga_title)

    # Nếu không có chương tiếng Việt (DMCA)
    if not all_chapters:
        notice_download = f"{base_url}/download_notice?title={safe_manga_title}"
        entries.append(f'''  <entry>
    <title>⚠️ BỘ TRUYỆN NÀY ĐÃ BỊ GỠ DO BẢN QUYỀN (DMCA)</title>
    <id>urn:dmca:notice:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bộ truyện "{xml_escape(manga_title)}" hiện chưa có chương tiếng Việt trên MangaDex. Hãy mở mục [Kho Google Drive Của Tôi] để đọc bộ này!</summary>
  </entry>''')
        entries.append(f'''  <entry>
    <title>📄 Tải Thông Báo Chi Tiết Về Máy</title>
    <id>urn:dmca:download:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bấm vào đây để tải tệp thông báo chi tiết về máy X4</summary>
    <link rel="http://opds-spec.org/acquisition" href="{notice_download}" type="application/epub+zip"/>
  </entry>''')
        return format_feed(base_url, f"manga:{manga_id}", manga_title, entries)

    sorted_asc = sorted(all_chapters, key=lambda c: parse_num(c.get('attributes', {}).get('chapter', '0')))
    sorted_desc = sorted(all_chapters, key=lambda c: parse_num(c.get('attributes', {}).get('chapter', '0')), reverse=True)
    first_ch = sorted_asc[0]
    latest_ch = sorted_desc[0]

    first_num = first_ch.get('attributes', {}).get('chapter') or '1'
    latest_num = latest_ch.get('attributes', {}).get('chapter') or 'Mới'

    # Tìm số chương
    if search_q:
        q_lower = search_q.strip().lower()
        matched = []
        for ch in sorted_desc:
            c_num = str(ch.get('attributes', {}).get('chapter') or '').lower()
            c_title = str(ch.get('attributes', {}).get('title') or '').lower()
            if q_lower == c_num or f"chương {q_lower}" in c_title or q_lower in c_title:
                matched.append(ch)

        back_url = f"{base_url}/opds/manga?id={manga_id}&amp;title={safe_manga_title}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Danh Mục Toàn Bộ Chương</title>
    <id>urn:mangadex:back:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Trở về danh sách toàn bộ chương</summary>
    <link rel="subsection" href="{back_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        if not matched:
            entries.append(f'''  <entry>
    <title>Không tìm thấy chương nào khớp với "{xml_escape(search_q)}"</title>
    <id>urn:mangadex:notfound:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Vui lòng thử gõ số chương khác hoặc bấm 'Quay Lại'</summary>
  </entry>''')
        else:
            for ch in matched:
                entries.append(create_chapter_entry(base_url, manga_title, ch))

        return format_feed(base_url, f"manga:{manga_id}", f"{manga_title} (Tìm số: {search_q})", entries)

    # Xem theo khoảng (Range)
    if range_param:
        try:
            start_idx, end_idx = map(int, range_param.split('-'))
        except Exception:
            start_idx, end_idx = 0, 40

        active_list = sorted_asc if order == 'asc' else sorted_desc
        subset = active_list[start_idx:end_idx]

        back_url = f"{base_url}/opds/manga?id={manga_id}&amp;title={safe_manga_title}&amp;order={order}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Toàn Bộ Chương</title>
    <id>urn:mangadex:back:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Trở về danh mục phân đoạn chương</summary>
    <link rel="subsection" href="{back_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        for ch in subset:
            entries.append(create_chapter_entry(base_url, manga_title, ch))

        r_title = f"{manga_title} (Khoảng {start_idx+1} - {min(end_idx, len(active_list))})"
        return format_feed(base_url, f"manga:{manga_id}", r_title, entries)

    # Chế độ Mặc Định
    first_download = f"{base_url}/download?chapter_id={first_ch['id']}&amp;title={urllib.parse.quote(f'{manga_title} - Chap {first_num}')}"
    entries.append(f'''  <entry>
    <title>⭐ [ĐỌC TỪ ĐẦU] Chương {first_num}</title>
    <id>urn:mangadex:readfirst:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải và đọc ngay từ chương đầu tiên</summary>
    <link rel="http://opds-spec.org/acquisition" href="{first_download}" type="application/epub+zip"/>
  </entry>''')

    latest_download = f"{base_url}/download?chapter_id={latest_ch['id']}&amp;title={urllib.parse.quote(f'{manga_title} - Chap {latest_num}')}"
    entries.append(f'''  <entry>
    <title>🔥 [ĐỌC MỚI NHẤT] Chương {latest_num}</title>
    <id>urn:mangadex:readlatest:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải và đọc ngay chương mới nhất</summary>
    <link rel="http://opds-spec.org/acquisition" href="{latest_download}" type="application/epub+zip"/>
  </entry>''')

    next_order = 'asc' if order == 'desc' else 'desc'
    order_label = "Từ Chương 1 -> Mới nhất" if order == 'desc' else "Mới nhất -> Cũ nhất"
    order_toggle_url = f"{base_url}/opds/manga?id={manga_id}&amp;order={next_order}&amp;title={safe_manga_title}"
    entries.append(f'''  <entry>
    <title>🔄 Đổi Thứ Tự: Xem [{order_label}]</title>
    <id>urn:mangadex:order:{manga_id}:{next_order}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bấm vào đây để đảo chiều danh sách chương</summary>
    <link rel="subsection" href="{order_toggle_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    active_list = sorted_asc if order == 'asc' else sorted_desc
    chunk_size = 40
    if len(active_list) > 35:
        for i in range(0, len(active_list), chunk_size):
            chunk = active_list[i:i+chunk_size]
            c_start = chunk[0].get('attributes', {}).get('chapter') or str(i+1)
            c_end = chunk[-1].get('attributes', {}).get('chapter') or str(i+len(chunk))
            range_url = f"{base_url}/opds/manga?id={manga_id}&amp;range={i}-{i+chunk_size}&amp;order={order}&amp;title={safe_manga_title}"
            entries.append(f'''  <entry>
    <title>📁 Khoảng: Chương {c_start} -> {c_end} ({len(chunk)} tập)</title>
    <id>urn:mangadex:range:{manga_id}:{i}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Mở danh sách gồm {len(chunk)} chương từ {c_start} đến {c_end}</summary>
    <link rel="subsection" href="{range_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    else:
        for ch in active_list:
            entries.append(create_chapter_entry(base_url, manga_title, ch))

    return format_feed(base_url, f"manga:{manga_id}", manga_title, entries)

def build_search_feed(base_url, query):
    entries = []
    try:
        md_items = fetch_mangadex_list("popular", query=query, limit=20)
        for item in md_items:
            m_id = item['id']
            attrs = item.get('attributes', {})
            raw_title = get_mangadex_title(attrs)
            safe_title = urllib.parse.quote(raw_title)

            cover_file = get_mangadex_cover(item.get('relationships', []))
            cover_xml = ""
            if cover_file:
                cover_url = f"{MANGADEX_COVER_BASE}/{m_id}/{cover_file}.256.jpg"
                cover_xml = f'''<link rel="http://opds-spec.org/image" href="{cover_url}" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{cover_url}" type="image/jpeg"/>'''

            desc_dict = attrs.get('description', {})
            desc_text = desc_dict.get('vi') or desc_dict.get('en') or 'Truyện tranh tiếng Việt'
            desc_clean = xml_escape(desc_text[:180] + ('...' if len(desc_text) > 180 else ''))

            entries.append(f'''  <entry>
    <title>{xml_escape(raw_title)}</title>
    <id>urn:search:manga:{m_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{desc_clean}</summary>
    {cover_xml}
    <link rel="subsection" href="{base_url}/opds/manga?id={m_id}&amp;title={safe_title}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    except Exception as e:
        print(f"[!] Search error: {e}")

    if not entries:
        entries.append(f'''  <entry>
    <title>Không tìm thấy truyện nào khớp với "{xml_escape(query)}"</title>
    <id>urn:search:empty</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Vui lòng thử gõ từ khóa khác ngắn gọn hơn</summary>
  </entry>''')

    return format_feed(base_url, f"search:{urllib.parse.quote(query)}", f"Tìm kiếm: {query}", entries)

# ------------------------------------------------------------------------------
# 5. HTTP Request Handler
# ------------------------------------------------------------------------------

class SafeMangaOPDSHandler(http.server.BaseHTTPRequestHandler):
    def get_base_url(self):
        host = self.headers.get('Host', 'xteink-manga.onrender.com')
        proto = self.headers.get('X-Forwarded-Proto', 'https')
        return f"{proto}://{host}"

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            base_url = self.get_base_url()

            # 1. Trang chủ OPDS
            if path in ['/', '/opds']:
                xml_data = build_root_feed(base_url)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 2. Google Drive Routing (Kho truyện cá nhân)
            elif path == '/opds/gdrive':
                xml_data = build_gdrive_folder_feed(base_url, GDRIVE_ROOT_FOLDER_ID, "Kho Google Drive Của Tôi")
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/gdrive/manga':
                folder_id = qs.get('folder_id', [GDRIVE_ROOT_FOLDER_ID])[0]
                manga_title = qs.get('title', ['Truyện Google Drive'])[0]
                xml_data = build_gdrive_folder_feed(base_url, folder_id, manga_title)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/download_gdrive':
                file_id = qs.get('file_id', [''])[0]
                title = qs.get('title', ['Manga'])[0]
                if not file_id:
                    self.send_error(400, "Missing file_id")
                    return
                epub_bytes = download_gdrive_file(file_id)
                safe_name = urllib.parse.quote(f"{title}.epub")
                self.send_response(200)
                self.send_header('Content-Type', 'application/epub+zip')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
                self.end_headers()
                self.wfile.write(epub_bytes)

            # 3. Gợi ý tìm kiếm nhanh
            elif path == '/opds/search_shortcuts':
                xml_data = build_search_shortcuts_feed(base_url)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 4. Tìm kiếm
            elif path == '/opds/search':
                q = qs.get('q', [''])[0].strip()
                xml_data = build_search_feed(base_url, q) if q else build_root_feed(base_url)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 5. Danh sách truyện MangaDex
            elif path in ['/opds/list', '/opds/mangadex/list', '/opds/nettruyen/list', '/opds/otruyen/list']:
                list_type = qs.get('type', ['popular'])[0]
                if list_type not in ['popular', 'rating', 'latest', 'completed']:
                    list_type = 'popular'

                title_map = {
                    'popular': 'MangaDex - Truyện Tiếng Việt Xem Nhiều',
                    'rating': 'MangaDex - Đánh Giá Cao Nhất',
                    'latest': 'MangaDex - Mới Cập Nhật',
                    'completed': 'MangaDex - Trọn Bộ Hoàn Thành'
                }
                feed_title = title_map.get(list_type, 'Kho Truyện Tranh')
                manga_list = fetch_mangadex_list(list_type)
                xml_data = build_manga_list_feed(base_url, feed_title, list_type, manga_list)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 6. Chi tiết chương MangaDex
            elif path in ['/opds/manga', '/opds/mangadex/manga', '/opds/nettruyen/manga', '/opds/otruyen/manga']:
                manga_id = qs.get('id', [''])[0]
                manga_title = qs.get('title', [''])[0]

                if not manga_id:
                    slug = qs.get('slug', [''])[0]
                    if slug or manga_title:
                        search_kw = manga_title or slug.replace('-', ' ')
                        found = fetch_mangadex_list("popular", query=search_kw, limit=1)
                        if found:
                            manga_id = found[0]['id']
                            if not manga_title:
                                manga_title = get_mangadex_title(found[0].get('attributes', {}))

                if not manga_id:
                    xml_data = build_root_feed(base_url)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                    self.end_headers()
                    self.wfile.write(xml_data.encode('utf-8'))
                    return

                if not manga_title:
                    try:
                        manga_title = fetch_mangadex_detail(manga_id)
                    except Exception:
                        manga_title = "Truyện tranh"

                order = qs.get('order', ['desc'])[0]
                range_param = qs.get('range', [''])[0]
                search_q = qs.get('q', [''])[0].strip()

                chapters = fetch_mangadex_chapters(manga_id)
                xml_data = build_chapter_feed(base_url, manga_id, manga_title, chapters, order=order, range_param=range_param, search_q=search_q)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 7. Tải tập truyện (MangaDex EPUB)
            elif path in ['/download', '/download_nettruyen', '/download_otruyen']:
                chapter_id = qs.get('chapter_id', [''])[0]
                title = qs.get('title', [f'Manga_Chapter'])[0]
                if not chapter_id:
                    self.send_error(400, "Missing chapter_id")
                    return
                final_title, epub_bytes = generate_mangadex_epub(chapter_id, title)
                safe_name = urllib.parse.quote(f"{final_title}.epub")
                self.send_response(200)
                self.send_header('Content-Type', 'application/epub+zip')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
                self.end_headers()
                self.wfile.write(epub_bytes)

            # 8. Tải thông báo bản quyền (Notice EPUB)
            elif path == '/download_notice':
                title = qs.get('title', ['Thong bao ban quyen'])[0]
                final_title, epub_bytes = generate_dmca_notice_epub(title)
                safe_name = urllib.parse.quote(f"{final_title}.epub")
                self.send_response(200)
                self.send_header('Content-Type', 'application/epub+zip')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
                self.end_headers()
                self.wfile.write(epub_bytes)

            else:
                self.send_error(404, "Not Found")

        except Exception as e:
            err_msg = traceback.format_exc()
            print(f"[!] Server Error: {err_msg}", file=sys.stderr)
            xml_err = f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Thông Báo</title>
  <entry>
    <title>Lỗi tạm thời: {xml_escape(str(e))}</title>
    <summary>Vui lòng thử lại sau giây lát hoặc chọn mục khác.</summary>
  </entry>
</feed>'''
            self.send_response(200)
            self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
            self.end_headers()
            self.wfile.write(xml_err.encode('utf-8'))

def run():
    port = int(os.environ.get('PORT', 7860))
    server_address = ('0.0.0.0', port)
    httpd = http.server.ThreadingHTTPServer(server_address, SafeMangaOPDSHandler)
    print(f"[*] Xteink Manga Server v7.0 running on port {port} (GDrive: {GDRIVE_ROOT_FOLDER_ID})...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
