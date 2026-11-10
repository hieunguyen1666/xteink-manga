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
import hashlib
import requests
from PIL import Image, ImageOps, ImageEnhance, ImageFilter, ImageDraw

# ==============================================================================
# Xteink X4 Manga OPDS Cloud Bridge - v5.0 Ultimate
# - Nguồn 1: NetTruyen (Kho truyện tiếng Việt lớn nhất, đầy đủ Solo Leveling, không DMCA)
# - Nguồn 2: MangaDex (Kho truyện tranh quốc tế)
# - Tự động phát hiện DMCA trên MangaDex -> Tự động chuyển link đọc sang NetTruyen
# - Tối ưu ảnh e-ink 480x800: Tự cắt viền trắng, chia đôi trang ngang, cắt dải cuộn manhwa
# - Bộ đệm Cache RAM + Đĩa cứng giúp tải siêu tốc, không bị lag
# ==============================================================================

NETTRUYEN_HOSTS = ["https://nettruyenar.com", "https://nettruyenww.com"]
NETTRUYEN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://nettruyenar.com/"
}

MANGADEX_API = "https://api.mangadex.org"
MANGADEX_COVER_BASE = "https://uploads.mangadex.org/covers"
MANGADEX_HEADERS = {
    "User-Agent": "XteinkMangaOPDS/5.0 (Xteink X4 E-Reader Bridge)",
    "Accept": "application/json"
}

# In-memory cache with TTL
RAM_CACHE = {}

# On-disk cache for EPUB chapters
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
# 1. Image Optimization & Slicing Pipeline (480x800 e-ink display)
# ------------------------------------------------------------------------------

def crop_margins(im, threshold=242, margin_pad=4):
    """Cắt viền trắng thừa quanh khung tranh để chữ to rõ hơn trên màn hình e-ink."""
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
        # Chỉ crop nếu vùng nội dung hợp lý (tránh mất hình nếu trang vẽ tối)
        if (right - left) > w * 0.60 and (lower - upper) > h * 0.60:
            return im.crop((left, upper, right, lower))
    return im

def optimize_and_split_image(img_bytes):
    """
    Biến đổi ảnh cho máy đọc sách Xteink X4 (480x800):
    - Cắt viền trắng
    - Trang đôi ngang -> Cắt đôi thành 2 trang dọc (Phải -> Trái) phóng to 200%
    - Manhwa cuộn dài -> Cắt thành các khung tile dọc tỉ lệ màn hình e-ink kèm overlap
    - Tăng tương phản và độ nét (Sharpen) để chữ đen rõ nét trên màn hình E-Ink
    """
    im = Image.open(io.BytesIO(img_bytes))
    if im.mode != 'L':
        im = ImageOps.grayscale(im)

    im = crop_margins(im)
    w, h = im.size
    sub_images = []

    if w > h * 1.25:
        # Trang đôi ngang -> Tách thành 2 trang dọc
        mid = w // 2
        right_page = crop_margins(im.crop((mid, 0, w, h)))
        left_page = crop_margins(im.crop((0, 0, mid, h)))
        sub_images.extend([right_page, left_page])
    elif h > w * 1.55:
        # Dải Manhwa / Webtoon cuộn dọc dài
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
    """Đóng gói mảng ảnh tối ưu thành tệp EPUB chuẩn đọc ngay trên CrossPoint Reader."""
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

def generate_dmca_notice_epub(manga_title, nettruyen_url=None):
    safe_title = f"Thong_bao_{manga_title[:20]}"
    esc_title = html.escape(manga_title)

    img = Image.new('L', (480, 800), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([15, 60, 465, 740], outline=0, width=4)
    draw.text((40, 100), "THONG BAO BAN QUYEN (DMCA)", fill=0)
    draw.line([40, 130, 440, 130], fill=0, width=2)
    draw.text((40, 160), "Bo truyen:", fill=0)
    draw.text((40, 190), f"{manga_title[:38]}", fill=0)
    draw.text((40, 250), "Chuong tren MangaDex da bi go", fill=0)
    draw.text((40, 285), "theo yeu cau ban quyen cua tac gia.", fill=0)
    draw.text((40, 350), "GIAI PHAP CO SAN:", fill=0)
    draw.text((40, 385), "- Nhan nut 'Quay lai' tren may X4", fill=0)
    draw.text((40, 420), "- Chon tiep muc '[TU DONG CHUYEN NGUON]'", fill=0)
    draw.text((40, 455), "  hoac mo kho 'NetTruyen' o Menu chinh", fill=0)
    draw.text((40, 490), "- Bo nay co san tron bo tren NetTruyen!", fill=0)

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    epub_bytes = build_epub_file("dmca_notice", esc_title, [buf.getvalue()])
    return safe_title, epub_bytes

# ------------------------------------------------------------------------------
# 2. NetTruyen Scraper & Engine (Full Vietnamese Content)
# ------------------------------------------------------------------------------

def make_nettruyen_request(path_or_url, is_ajax=False, timeout=12):
    """Gửi yêu cầu đến NetTruyen với cơ chế tự động chuyển đổi host khi cần."""
    last_err = None
    for host in NETTRUYEN_HOSTS:
        url = path_or_url if path_or_url.startswith('http') else f"{host}{path_or_url}"
        headers = dict(NETTRUYEN_HEADERS)
        headers['Referer'] = f"{host}/"
        if is_ajax:
            headers['X-Requested-With'] = 'XMLHttpRequest'
        try:
            res = requests.get(url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                return res
        except Exception as e:
            last_err = e
            continue
    raise ConnectionError(f"Không thể kết nối đến NetTruyen: {last_err}")

def parse_nettruyen_items(html_text):
    """Trích xuất danh sách truyện từ mã nguồn HTML NetTruyen."""
    items = []
    blocks = re.findall(r'<div class="item"[^>]*>([\s\S]*?)</figcaption>', html_text)
    for b in blocks:
        h3 = re.search(r'<h3[^>]*>([\s\S]*?)</h3>', b)
        thumb = re.search(r'data-original="([^"]+)"', b)
        if h3:
            a = re.search(r'href="([^"]+)"[^>]*>([^<]+)</a>', h3.group(1))
            if a:
                href = a.group(1)
                title = a.group(2).strip()
                m = re.search(r'/truyen-tranh/(?P<slug>.+)-(?P<id>\d+)', href)
                if m:
                    slug = m.group('slug')
                    comic_id = m.group('id')
                    
                    # Tìm chapter mới nhất
                    ch_match = re.search(r'<a[^>]*href="[^"]*/chapter-[^"]*"[^>]*>([^<]+)</a>', b)
                    latest_ch = ch_match.group(1).strip() if ch_match else ''
                    
                    thumb_url = thumb.group(1) if thumb else ''
                    if thumb_url.startswith('//'):
                        thumb_url = 'https:' + thumb_url

                    items.append({
                        'title': title,
                        'slug': slug,
                        'id': comic_id,
                        'url': href,
                        'thumb': thumb_url,
                        'latest_chapter': latest_ch
                    })
    return items

def fetch_nettruyen_list(list_type, page=1):
    cache_key = f"nt_list_{list_type}_{page}"
    now = time.time()
    if cache_key in RAM_CACHE:
        ts, data = RAM_CACHE[cache_key]
        if now - ts < 600:
            return data

    if list_type == 'hot':
        path = f"/truyen-tranh-hot?page={page}"
    else:
        path = f"/?page={page}"

    res = make_nettruyen_request(path)
    items = parse_nettruyen_items(res.text)
    
    # Tìm tổng số trang nếu có
    total_pages = 500
    page_matches = re.findall(r'[?&]page=(\d+)', res.text)
    if page_matches:
        try:
            total_pages = max(int(p) for p in page_matches)
        except Exception:
            pass

    result = (items, total_pages)
    RAM_CACHE[cache_key] = (now, result)
    return result

def fetch_nettruyen_search(keyword, page=1):
    q_enc = urllib.parse.quote_plus(keyword)
    path = f"/tim-truyen?keyword={q_enc}&page={page}"
    res = make_nettruyen_request(path)
    return parse_nettruyen_items(res.text)

def fetch_nettruyen_chapters(slug, comic_id):
    """Lấy danh sách toàn bộ chương từ AJAX service của NetTruyen."""
    cache_key = f"nt_ch_{comic_id}"
    now = time.time()
    if cache_key in RAM_CACHE:
        ts, data = RAM_CACHE[cache_key]
        if now - ts < 900:
            return data

    path = f"/Comic/Services/ComicService.asmx/ChapterList?slug={slug}&comicId={comic_id}"
    res = make_nettruyen_request(path, is_ajax=True)
    payload = res.json()
    chapters = payload.get('data', [])
    RAM_CACHE[cache_key] = (now, chapters)
    return chapters

def fetch_nettruyen_chapter_images(slug, chapter_slug, chapter_id):
    """Lấy danh sách URL ảnh trong 1 chương của NetTruyen."""
    path = f"/truyen-tranh/{slug}/{chapter_slug}/{chapter_id}"
    res = make_nettruyen_request(path)
    html_text = res.text

    imgs = re.findall(r'<div class=[\'"]page-chapter[\'"][^>]*>[\s\S]*?<img[^>]*data-src=[\'"]([^\'"]+)[\'"]', html_text)
    if not imgs:
        imgs = re.findall(r'<div class=[\'"]page-chapter[\'"][^>]*>[\s\S]*?<img[^>]*data-original=[\'"]([^\'"]+)[\'"]', html_text)
    if not imgs:
        imgs = re.findall(r'<div class=[\'"]page-chapter[\'"][^>]*>[\s\S]*?<img[^>]*data-sv1=[\'"]([^\'"]+)[\'"]', html_text)
    if not imgs:
        imgs = re.findall(r'data-original=[\'"]([^\'"]+\.(?:jpg|jpeg|png|webp))[\'"]', html_text)

    clean_imgs = []
    for u in imgs:
        if u.startswith('//'):
            clean_imgs.append('https:' + u)
        elif u.startswith('http'):
            clean_imgs.append(u)
    return clean_imgs

def generate_nettruyen_epub(slug, chapter_slug, chapter_id, chapter_title):
    """Tải và đóng gói chương truyện NetTruyen thành file EPUB tối ưu cho máy đọc sách."""
    disk_file = os.path.join(DISK_CACHE_DIR, f"nt_{chapter_id}.epub")
    if os.path.exists(disk_file) and os.path.getsize(disk_file) > 1024:
        print(f"[*] Phục vụ từ Cache đĩa cứng: {disk_file}")
        with open(disk_file, 'rb') as f:
            return chapter_title, f.read()

    images = fetch_nettruyen_chapter_images(slug, chapter_slug, chapter_id)
    if not images:
        raise ValueError(f"Không tìm thấy hình ảnh nào trong chương '{chapter_title}' trên NetTruyen.")

    processed_images = []
    print(f"[*] [NetTruyen] Đang tải và xử lý {len(images)} trang ảnh cho: {chapter_title}...")
    headers = {
        "User-Agent": NETTRUYEN_HEADERS["User-Agent"],
        "Referer": "https://nettruyenar.com/"
    }

    for idx, img_url in enumerate(images):
        try:
            r = requests.get(img_url, headers=headers, timeout=15)
            if r.status_code == 200:
                buffers = optimize_and_split_image(r.content)
                processed_images.extend(buffers)
        except Exception as e:
            print(f"[!] Lỗi tải ảnh {idx+1}: {e}")

    if not processed_images:
        raise ValueError("Không thể tải được trang ảnh hợp lệ nào từ chương này.")

    safe_title = chapter_title or f"Chapter_{chapter_id}"
    epub_bytes = build_epub_file(f"nt_{chapter_id}", safe_title, processed_images)

    # Lưu vào đĩa cache
    try:
        with open(disk_file, 'wb') as f:
            f.write(epub_bytes)
    except Exception as e:
        print(f"[!] Không thể lưu cache: {e}")

    return safe_title, epub_bytes

# ------------------------------------------------------------------------------
# 3. MangaDex Scraper & Engine (Global Content)
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
    else:
        params["order[followedCount]"] = "desc"

    res = requests.get(f"{MANGADEX_API}/manga", headers=MANGADEX_HEADERS, params=params, timeout=15)
    res.raise_for_status()
    return res.json().get('data', [])

def fetch_mangadex_detail(manga_id):
    res = requests.get(f"{MANGADEX_API}/manga/{manga_id}", headers=MANGADEX_HEADERS, timeout=15)
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
    res = requests.get(f"{MANGADEX_API}/manga/{manga_id}/feed", headers=MANGADEX_HEADERS, params=params, timeout=15)
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

    res = requests.get(f"{MANGADEX_API}/at-home/server/{chapter_id}", headers=MANGADEX_HEADERS, timeout=15)
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
            r = requests.get(img_url, headers=MANGADEX_HEADERS, timeout=15)
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
# 4. OPDS Feeds Construction
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
  <author><name>Xteink Cloud Bridge v5.0</name></author>
  <link rel="self" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>

  <entry>
    <title>🔍 Tìm Kiếm Nhanh (Gõ Tên / Gợi Ý Hot)</title>
    <id>urn:xteink:search_menu</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tìm kiếm nhanh trên toàn bộ kho truyện tiếng Việt</summary>
    <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>
    <link rel="subsection" href="{base_url}/opds/search_shortcuts" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  </entry>

  <entry>
    <title>🌟 [NetTruyen] Truyện Mới Cập Nhật (Đầy Đủ, Không DMCA)</title>
    <id>urn:xteink:nettruyen:new</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Kho truyện tiếng Việt phong phú nhất, cập nhật liên tục từng giờ</summary>
    <link rel="subsection" href="{base_url}/opds/nettruyen/list?type=new" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🔥 [NetTruyen] Truyện Hot / Xem Nhiều (Solo Leveling...)</title>
    <id>urn:xteink:nettruyen:hot</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Các bộ truyện tranh đỉnh cao: Tôi Thăng Cấp Một Mình, Manhwa, Shounen...</summary>
    <link rel="subsection" href="{base_url}/opds/nettruyen/list?type=hot" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🌐 [MangaDex] Truyện Quốc Tế Xem Nhiều</title>
    <id>urn:xteink:mangadex:popular</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Kho truyện tranh toàn cầu có bản dịch tiếng Việt</summary>
    <link rel="subsection" href="{base_url}/opds/mangadex/list?type=popular" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>⭐ [MangaDex] Đánh Giá Cao Nhất</title>
    <id>urn:xteink:mangadex:rating</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Các bộ truyện được cộng đồng quốc tế đánh giá cao nhất</summary>
    <link rel="subsection" href="{base_url}/opds/mangadex/list?type=rating" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>
</feed>'''

def build_search_shortcuts_feed(base_url):
    keywords = [
        "Tôi Thăng Cấp Một Mình", "Solo Leveling", "One Piece", "Conan", "Doraemon",
        "Naruto", "Dragon Ball", "Jujutsu Kaisen", "Chainsaw Man", "Kimetsu no Yaiba",
        "Attack on Titan", "Bleach", "Hunter x Hunter", "Fairy Tail", "Spy x Family",
        "Berserk", "Tokyo Ghoul", "Black Clover", "Haikyuu", "Slam Dunk", "Death Note"
    ]
    entries = []
    for kw in keywords:
        q_enc = urllib.parse.quote(kw)
        entries.append(f'''  <entry>
    <title>📖 {kw}</title>
    <id>urn:xteink:search:{q_enc}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Xem kết quả tìm kiếm cho bộ "{kw}"</summary>
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

# --- NetTruyen OPDS Feeds ---

def build_nettruyen_list_feed(base_url, feed_title, list_type, items, page=1, total_pages=500):
    entries = []
    for item in items:
        title = item['title']
        slug = item['slug']
        c_id = item['id']
        thumb = item['thumb']
        latest = item.get('latest_chapter', '')

        summary = f"Mới nhất: {latest}" if latest else "Truyện tranh NetTruyen tiếng Việt"
        cover_xml = ""
        if thumb:
            cover_xml = f'''<link rel="http://opds-spec.org/image" href="{thumb}" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{thumb}" type="image/jpeg"/>'''

        safe_slug = urllib.parse.quote(slug)
        safe_title = urllib.parse.quote(title)
        manga_url = f"{base_url}/opds/nettruyen/manga?slug={safe_slug}&amp;id={c_id}&amp;title={safe_title}"

        entries.append(f'''  <entry>
    <title>{xml_escape(title)}</title>
    <id>urn:nettruyen:manga:{c_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{xml_escape(summary)}</summary>
    {cover_xml}
    <link rel="subsection" href="{manga_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    if page < total_pages:
        next_page = page + 1
        next_url = f"{base_url}/opds/nettruyen/list?type={list_type}&amp;page={next_page}"
        entries.append(f'''  <entry>
    <title>⏩ Trang Tiếp Theo (Trang {next_page} / {total_pages})</title>
    <id>urn:nettruyen:page:{list_type}:{next_page}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Xem thêm các bộ truyện ở trang sau</summary>
    <link rel="subsection" href="{next_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:nettruyen:list:{list_type}:{page}</id>
  <title>{xml_escape(feed_title)} (Trang {page})</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

def create_nettruyen_chapter_entry(base_url, slug, manga_title, ch):
    ch_id = ch.get('chapter_id')
    ch_name = ch.get('chapter_name', '1')
    ch_slug = ch.get('chapter_slug', f"chapter-{ch_id}")
    
    full_title = f"{ch_name}"
    esc_title = xml_escape(full_title)

    safe_title = urllib.parse.quote(f"{manga_title} - {ch_name}")
    download_url = f"{base_url}/download_nettruyen?slug={urllib.parse.quote(slug)}&amp;chapter_slug={urllib.parse.quote(ch_slug)}&amp;chapter_id={ch_id}&amp;title={safe_title}"

    return f'''  <entry>
    <title>{esc_title}</title>
    <id>urn:nettruyen:chapter:{ch_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải và đọc chương {ch_name} (NetTruyen)</summary>
    <link rel="http://opds-spec.org/acquisition" href="{download_url}" type="application/epub+zip"/>
  </entry>'''

def build_nettruyen_chapter_feed(base_url, slug, comic_id, manga_title, chapters, order='desc', range_param=None, search_q=None):
    entries = []
    safe_slug = urllib.parse.quote(slug)
    safe_title = urllib.parse.quote(manga_title)

    if not chapters:
        entries.append(f'''  <entry>
    <title>Chưa có chương nào sẵn sàng</title>
    <id>urn:nettruyen:empty:{comic_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bộ truyện này hiện chưa có tập truyện nào được cập nhật trên NetTruyen.</summary>
  </entry>''')
        return format_feed(base_url, f"nettruyen:{comic_id}", manga_title, entries)

    sorted_asc = sorted(chapters, key=lambda c: parse_num(c.get('chapter_name', '0')))
    sorted_desc = sorted(chapters, key=lambda c: parse_num(c.get('chapter_name', '0')), reverse=True)
    first_ch = sorted_asc[0]
    latest_ch = sorted_desc[0]

    first_num = first_ch.get('chapter_name', '1')
    latest_num = latest_ch.get('chapter_name', 'Mới')

    # 1. Tìm kiếm số chương
    if search_q:
        q_lower = search_q.strip().lower()
        matched = []
        for ch in sorted_desc:
            c_name = str(ch.get('chapter_name', '')).lower()
            if q_lower == c_name or f"chapter {q_lower}" == c_name or f"chương {q_lower}" == c_name or q_lower in c_name:
                matched.append(ch)

        back_url = f"{base_url}/opds/nettruyen/manga?slug={safe_slug}&amp;id={comic_id}&amp;title={safe_title}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Danh Mục Chương</title>
    <id>urn:nettruyen:back:{comic_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Xem toàn bộ danh mục chương</summary>
    <link rel="subsection" href="{back_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        if not matched:
            entries.append(f'''  <entry>
    <title>Không tìm thấy chương nào khớp với "{xml_escape(search_q)}"</title>
    <id>urn:nettruyen:notfound:{comic_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Vui lòng thử gõ số chương khác hoặc bấm 'Quay Lại'</summary>
  </entry>''')
        else:
            for ch in matched:
                entries.append(create_nettruyen_chapter_entry(base_url, slug, manga_title, ch))
        return format_feed(base_url, f"nettruyen:{comic_id}", f"{manga_title} (Tìm số: {search_q})", entries)

    # 2. Xem theo khoảng chương (Range)
    if range_param:
        try:
            start_idx, end_idx = map(int, range_param.split('-'))
        except Exception:
            start_idx, end_idx = 0, 40

        active_list = sorted_asc if order == 'asc' else sorted_desc
        subset = active_list[start_idx:end_idx]

        back_url = f"{base_url}/opds/nettruyen/manga?slug={safe_slug}&amp;id={comic_id}&amp;title={safe_title}&amp;order={order}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Toàn Bộ Chương</title>
    <id>urn:nettruyen:back:{comic_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Trở về danh mục phân đoạn chương</summary>
    <link rel="subsection" href="{back_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        for ch in subset:
            entries.append(create_nettruyen_chapter_entry(base_url, slug, manga_title, ch))

        r_title = f"{manga_title} (Khoảng {start_idx+1} - {min(end_idx, len(active_list))})"
        return format_feed(base_url, f"nettruyen:{comic_id}", r_title, entries)

    # 3. Chế độ Mặc Định: Phím tắt + Phân đoạn
    first_download = f"{base_url}/download_nettruyen?slug={safe_slug}&amp;chapter_slug={urllib.parse.quote(first_ch.get('chapter_slug', ''))}&amp;chapter_id={first_ch.get('chapter_id')}&amp;title={urllib.parse.quote(f'{manga_title} - {first_num}')}"
    entries.append(f'''  <entry>
    <title>⭐ [ĐỌC TỪ ĐẦU] {first_num}</title>
    <id>urn:nettruyen:readfirst:{comic_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải và đọc ngay từ chương đầu tiên</summary>
    <link rel="http://opds-spec.org/acquisition" href="{first_download}" type="application/epub+zip"/>
  </entry>''')

    latest_download = f"{base_url}/download_nettruyen?slug={safe_slug}&amp;chapter_slug={urllib.parse.quote(latest_ch.get('chapter_slug', ''))}&amp;chapter_id={latest_ch.get('chapter_id')}&amp;title={urllib.parse.quote(f'{manga_title} - {latest_num}')}"
    entries.append(f'''  <entry>
    <title>🔥 [ĐỌC MỚI NHẤT] {latest_num}</title>
    <id>urn:nettruyen:readlatest:{comic_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải và đọc ngay chương mới nhất</summary>
    <link rel="http://opds-spec.org/acquisition" href="{latest_download}" type="application/epub+zip"/>
  </entry>''')

    next_order = 'asc' if order == 'desc' else 'desc'
    order_label = "Từ Chương 1 -> Mới nhất" if order == 'desc' else "Mới nhất -> Cũ nhất"
    order_toggle_url = f"{base_url}/opds/nettruyen/manga?slug={safe_slug}&amp;id={comic_id}&amp;order={next_order}&amp;title={safe_title}"
    entries.append(f'''  <entry>
    <title>🔄 Đổi Thứ Tự: Xem [{order_label}]</title>
    <id>urn:nettruyen:order:{comic_id}:{next_order}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bấm vào đây để đảo chiều danh sách chương</summary>
    <link rel="subsection" href="{order_toggle_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    active_list = sorted_asc if order == 'asc' else sorted_desc
    chunk_size = 40
    if len(active_list) > 35:
        for i in range(0, len(active_list), chunk_size):
            chunk = active_list[i:i+chunk_size]
            c_start = chunk[0].get('chapter_name') or str(i+1)
            c_end = chunk[-1].get('chapter_name') or str(i+len(chunk))
            range_url = f"{base_url}/opds/nettruyen/manga?slug={safe_slug}&amp;id={comic_id}&amp;range={i}-{i+chunk_size}&amp;order={order}&amp;title={safe_title}"
            entries.append(f'''  <entry>
    <title>📁 Khoảng: {c_start} -> {c_end} ({len(chunk)} tập)</title>
    <id>urn:nettruyen:range:{comic_id}:{i}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Mở danh sách gồm {len(chunk)} chương từ {c_start} đến {c_end}</summary>
    <link rel="subsection" href="{range_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    else:
        for ch in active_list:
            entries.append(create_nettruyen_chapter_entry(base_url, slug, manga_title, ch))

    return format_feed(base_url, f"nettruyen:{comic_id}", manga_title, entries)

# --- MangaDex OPDS Feeds ---

def build_mangadex_list_feed(base_url, feed_title, list_type, manga_list):
    entries = []
    for item in manga_list:
        m_id = item['id']
        attrs = item.get('attributes', {})
        raw_title = get_mangadex_title(attrs)
        title = xml_escape(raw_title)

        desc_dict = attrs.get('description', {})
        desc_text = desc_dict.get('vi') or desc_dict.get('en') or 'Truyện tranh MangaDex tiếng Việt'
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
    <link rel="subsection" href="{base_url}/opds/mangadex/manga?id={m_id}&amp;title={safe_manga_title}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:mangadex:list:{list_type}</id>
  <title>{xml_escape(feed_title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

def create_mangadex_chapter_entry(base_url, manga_title, ch):
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

def build_mangadex_chapter_feed(base_url, manga_id, manga_title, all_chapters, order='desc', range_param=None, search_q=None):
    entries = []
    safe_manga_title = urllib.parse.quote(manga_title)

    # Smart Fallback nếu MangaDex bị gỡ chương do DMCA hoặc không có chương tiếng Việt
    if not all_chapters:
        nt_match = None
        try:
            nt_items = fetch_nettruyen_search(manga_title)
            if nt_items:
                nt_match = nt_items[0]
        except Exception:
            pass

        if nt_match:
            nt_slug = nt_match['slug']
            nt_id = nt_match['id']
            nt_name = nt_match['title']
            nt_url = f"{base_url}/opds/nettruyen/manga?slug={urllib.parse.quote(nt_slug)}&amp;id={nt_id}&amp;title={urllib.parse.quote(nt_name)}"
            entries.append(f'''  <entry>
    <title>✨ [TỰ ĐỘNG CHUYỂN NGUỒN] Đọc Trọn Bộ Trên NetTruyen</title>
    <id>urn:fallback:nettruyen:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bộ truyện "{xml_escape(manga_title)}" bị khóa bản quyền trên MangaDex nhưng ĐÃ ĐƯỢC TÌM THẤY trên NetTruyen ({xml_escape(nt_name)}). Bấm vào đây để mở trọn bộ đọc ngay!</summary>
    <link rel="subsection" href="{nt_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        notice_url = f"{base_url}/download_notice?title={safe_manga_title}"
        entries.append(f'''  <entry>
    <title>⚠️ Đã bị gỡ trên MangaDex do bản quyền DMCA</title>
    <id>urn:mangadex:empty:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bấm vào đây để tải thông báo chi tiết và hướng dẫn chuyển sang nguồn NetTruyen.</summary>
    <link rel="http://opds-spec.org/acquisition" href="{notice_url}" type="application/epub+zip"/>
  </entry>''')
        return format_feed(base_url, f"mangadex:{manga_id}", manga_title, entries)

    sorted_asc = sorted(all_chapters, key=lambda c: parse_num(c.get('attributes', {}).get('chapter', '0')))
    sorted_desc = sorted(all_chapters, key=lambda c: parse_num(c.get('attributes', {}).get('chapter', '0')), reverse=True)
    first_ch = sorted_asc[0]
    latest_ch = sorted_desc[0]

    first_num = first_ch.get('attributes', {}).get('chapter') or '1'
    latest_num = latest_ch.get('attributes', {}).get('chapter') or 'Mới'

    # 1. Tìm số chương
    if search_q:
        q_lower = search_q.strip().lower()
        matched = []
        for ch in sorted_desc:
            c_num = str(ch.get('attributes', {}).get('chapter') or '').lower()
            c_title = str(ch.get('attributes', {}).get('title') or '').lower()
            if q_lower == c_num or f"chương {q_lower}" in c_title or q_lower in c_title:
                matched.append(ch)

        back_url = f"{base_url}/opds/mangadex/manga?id={manga_id}&amp;title={safe_manga_title}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Danh Mục Chương</title>
    <id>urn:mangadex:back:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Xem toàn bộ danh mục chương</summary>
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
                entries.append(create_mangadex_chapter_entry(base_url, manga_title, ch))

        return format_feed(base_url, f"mangadex:{manga_id}", f"{manga_title} (Tìm số: {search_q})", entries)

    # 2. Xem theo khoảng chương (Range)
    if range_param:
        try:
            start_idx, end_idx = map(int, range_param.split('-'))
        except Exception:
            start_idx, end_idx = 0, 40

        active_list = sorted_asc if order == 'asc' else sorted_desc
        subset = active_list[start_idx:end_idx]

        back_url = f"{base_url}/opds/mangadex/manga?id={manga_id}&amp;title={safe_manga_title}&amp;order={order}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Toàn Bộ Chương</title>
    <id>urn:mangadex:back:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Trở về danh mục phân đoạn chương</summary>
    <link rel="subsection" href="{back_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        for ch in subset:
            entries.append(create_mangadex_chapter_entry(base_url, manga_title, ch))

        r_title = f"{manga_title} (Khoảng {start_idx+1} - {min(end_idx, len(active_list))})"
        return format_feed(base_url, f"mangadex:{manga_id}", r_title, entries)

    # 3. Chế độ Mặc Định
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
    order_toggle_url = f"{base_url}/opds/mangadex/manga?id={manga_id}&amp;order={next_order}&amp;title={safe_manga_title}"
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
            range_url = f"{base_url}/opds/mangadex/manga?id={manga_id}&amp;range={i}-{i+chunk_size}&amp;order={order}&amp;title={safe_manga_title}"
            entries.append(f'''  <entry>
    <title>📁 Khoảng: Chương {c_start} -> {c_end} ({len(chunk)} tập)</title>
    <id>urn:mangadex:range:{manga_id}:{i}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Mở danh sách gồm {len(chunk)} chương từ {c_start} đến {c_end}</summary>
    <link rel="subsection" href="{range_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    else:
        for ch in active_list:
            entries.append(create_mangadex_chapter_entry(base_url, manga_title, ch))

    return format_feed(base_url, f"mangadex:{manga_id}", manga_title, entries)

# --- Unified Search Feed ---

def build_unified_search_feed(base_url, query):
    entries = []

    # 1. Tìm trên NetTruyen (Ưu tiên số 1 vì kho truyện tiếng Việt cực lớn & không DMCA)
    try:
        nt_items = fetch_nettruyen_search(query)
        for item in nt_items[:20]:
            title = item['title']
            slug = item['slug']
            c_id = item['id']
            thumb = item['thumb']
            safe_slug = urllib.parse.quote(slug)
            safe_title = urllib.parse.quote(title)

            cover_xml = ""
            if thumb:
                cover_xml = f'''<link rel="http://opds-spec.org/image" href="{thumb}" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{thumb}" type="image/jpeg"/>'''

            manga_url = f"{base_url}/opds/nettruyen/manga?slug={safe_slug}&amp;id={c_id}&amp;title={safe_title}"
            entries.append(f'''  <entry>
    <title>[NetTruyen] {xml_escape(title)}</title>
    <id>urn:search:nettruyen:{c_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Nguồn NetTruyen (Đầy đủ, không sợ bản quyền)</summary>
    {cover_xml}
    <link rel="subsection" href="{manga_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    except Exception as e:
        print(f"[!] NetTruyen search error: {e}")

    # 2. Tìm trên MangaDex
    try:
        md_items = fetch_mangadex_list("popular", query=query, limit=15)
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

            entries.append(f'''  <entry>
    <title>[MangaDex] {xml_escape(raw_title)}</title>
    <id>urn:search:mangadex:{m_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Nguồn MangaDex Quốc Tế</summary>
    {cover_xml}
    <link rel="subsection" href="{base_url}/opds/mangadex/manga?id={m_id}&amp;title={safe_title}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    except Exception as e:
        print(f"[!] MangaDex search error: {e}")

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

class MultiSourceMangaHandler(http.server.BaseHTTPRequestHandler):
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

            # 1. Trang chủ & Tìm kiếm
            if path in ['/', '/opds']:
                xml_data = build_root_feed(base_url)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/search_shortcuts':
                xml_data = build_search_shortcuts_feed(base_url)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/search':
                q = qs.get('q', [''])[0].strip()
                xml_data = build_unified_search_feed(base_url, q) if q else build_root_feed(base_url)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 2. NetTruyen Routing (Kèm tự động chuyển tiếp từ link cũ OTruyen)
            elif path in ['/opds/nettruyen/list', '/opds/otruyen/list']:
                list_type = qs.get('type', ['new'])[0]
                if list_type in ['truyen-moi', 'dang-phat-hanh']:
                    list_type = 'new'
                elif list_type in ['hoan-thanh']:
                    list_type = 'hot'

                page = int(qs.get('page', ['1'])[0])
                title_map = {
                    'hot': 'NetTruyen - Truyện Hot Xem Nhiều',
                    'new': 'NetTruyen - Mới Cập Nhật'
                }
                feed_title = title_map.get(list_type, 'NetTruyen')
                items, total_pages = fetch_nettruyen_list(list_type, page=page)
                xml_data = build_nettruyen_list_feed(base_url, feed_title, list_type, items, page=page, total_pages=total_pages)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path in ['/opds/nettruyen/manga', '/opds/otruyen/manga']:
                slug = qs.get('slug', [''])[0]
                comic_id = qs.get('id', [''])[0]
                manga_title = qs.get('title', [''])[0]

                # Nếu gọi từ link OTruyen cũ không có comic_id -> tự động tìm trên NetTruyen
                if not comic_id and (slug or manga_title):
                    search_kw = manga_title or slug.replace('-', ' ')
                    found = fetch_nettruyen_search(search_kw)
                    if found:
                        slug = found[0]['slug']
                        comic_id = found[0]['id']
                        if not manga_title:
                            manga_title = found[0]['title']

                if not comic_id:
                    self.send_error(400, "Missing comic id")
                    return

                order = qs.get('order', ['desc'])[0]
                range_param = qs.get('range', [''])[0]
                search_q = qs.get('q', [''])[0].strip()

                chapters = fetch_nettruyen_chapters(slug, comic_id)
                xml_data = build_nettruyen_chapter_feed(base_url, slug, comic_id, manga_title, chapters, order=order, range_param=range_param, search_q=search_q)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 3. MangaDex Routing (Kèm tương thích ngược)
            elif path in ['/opds/mangadex/list', '/opds/list']:
                list_type = qs.get('type', ['popular'])[0]
                title_map = {
                    'popular': 'MangaDex - Phổ Biến Nhất',
                    'rating': 'MangaDex - Đánh Giá Cao',
                    'latest': 'MangaDex - Mới Cập Nhật'
                }
                feed_title = title_map.get(list_type, 'MangaDex')
                manga_list = fetch_mangadex_list(list_type)
                xml_data = build_mangadex_list_feed(base_url, feed_title, list_type, manga_list)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path in ['/opds/mangadex/manga', '/opds/manga']:
                manga_id = qs.get('id', [''])[0]
                slug = qs.get('slug', [''])[0]

                # Nếu truyền slug -> chuyển sang NetTruyen
                if slug:
                    comic_id = qs.get('id', [''])[0]
                    manga_title = qs.get('title', [''])[0]
                    if not comic_id:
                        found = fetch_nettruyen_search(manga_title or slug.replace('-', ' '))
                        if found:
                            slug = found[0]['slug']
                            comic_id = found[0]['id']
                            manga_title = found[0]['title']
                    chapters = fetch_nettruyen_chapters(slug, comic_id) if comic_id else []
                    xml_data = build_nettruyen_chapter_feed(base_url, slug, comic_id, manga_title, chapters)
                else:
                    if not manga_id:
                        self.send_error(400, "Missing manga id")
                        return
                    manga_title = qs.get('title', [''])[0]
                    if not manga_title:
                        try:
                            manga_title = fetch_mangadex_detail(manga_id)
                        except Exception:
                            manga_title = "Truyện tranh"

                    order = qs.get('order', ['desc'])[0]
                    range_param = qs.get('range', [''])[0]
                    search_q = qs.get('q', [''])[0].strip()

                    chapters = fetch_mangadex_chapters(manga_id)
                    xml_data = build_mangadex_chapter_feed(base_url, manga_id, manga_title, chapters, order=order, range_param=range_param, search_q=search_q)

                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 4. Downloads
            elif path in ['/download_nettruyen', '/download_otruyen']:
                slug = qs.get('slug', [''])[0]
                chapter_slug = qs.get('chapter_slug', [''])[0]
                chapter_id = qs.get('chapter_id', [''])[0]
                title = qs.get('title', ['Manga_Chapter'])[0]

                if not chapter_id or not slug:
                    self.send_error(400, "Missing slug or chapter_id")
                    return

                final_title, epub_bytes = generate_nettruyen_epub(slug, chapter_slug, chapter_id, title)
                safe_name = urllib.parse.quote(f"{final_title}.epub")
                self.send_response(200)
                self.send_header('Content-Type', 'application/epub+zip')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
                self.end_headers()
                self.wfile.write(epub_bytes)

            elif path == '/download':
                chapter_id = qs.get('chapter_id', [''])[0]
                title = qs.get('title', [f'Manga_{chapter_id}'])[0]
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

            elif path == '/download_notice':
                title = qs.get('title', ['Thong bao'])[0]
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
  <title>Lỗi Máy Chủ</title>
  <entry>
    <title>Lỗi: {xml_escape(str(e))}</title>
    <summary>{xml_escape(err_msg)}</summary>
  </entry>
</feed>'''
            self.send_response(200)
            self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
            self.end_headers()
            self.wfile.write(xml_err.encode('utf-8'))

def run():
    port = int(os.environ.get('PORT', 7860))
    server_address = ('0.0.0.0', port)
    httpd = http.server.ThreadingHTTPServer(server_address, MultiSourceMangaHandler)
    print(f"[*] Xteink Manga Server v5.0 (NetTruyen + MangaDex) running on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
