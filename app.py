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

OTRUYEN_API = "https://otruyenapi.com/v1/api"
OTRUYEN_IMG_BASE = "https://img.otruyenapi.com/uploads/comics"
MANGADEX_API = "https://api.mangadex.org"
MANGADEX_COVER_BASE = "https://uploads.mangadex.org/covers"

HEADERS = {
    "User-Agent": "XteinkMangaOPDS/4.0 (Xteink X4 E-Reader Bridge; https://github.com/hieunguyen1666/xteink-manga)",
    "Accept": "application/json"
}

# In-memory cache with 15-minute TTL
CACHE = {}

def xml_escape(text):
    return html.escape(text or '')

def get_otruyen_thumb(thumb_url):
    if not thumb_url:
        return ""
    if thumb_url.startswith('http'):
        return thumb_url
    clean_thumb = thumb_url.lstrip('/')
    if clean_thumb.startswith('uploads/comics/'):
        return f"https://img.otruyenapi.com/{clean_thumb}"
    return f"{OTRUYEN_IMG_BASE}/{clean_thumb}"

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

def parse_num(val):
    try:
        m = re.search(r'(\d+(\.\d+)?)', str(val))
        return float(m.group(1)) if m else -1.0
    except:
        return -1.0

# ----------------- Image Optimization & Slicing (480x800 e-ink) -----------------

def crop_margins(im, threshold=242, margin_pad=4):
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
        if (right - left) > w * 0.65 and (lower - upper) > h * 0.65:
            return im.crop((left, upper, right, lower))
    return im

def optimize_and_split_image(img_bytes):
    im = Image.open(io.BytesIO(img_bytes))
    if im.mode != 'L':
        im = ImageOps.grayscale(im)
    
    im = crop_margins(im)
    w, h = im.size
    sub_images = []
    
    if w > h * 1.25:
        # Trang đôi nằm ngang -> Cắt đôi (Phải trước, Trái sau) phóng to 200%
        mid = w // 2
        right_page = crop_margins(im.crop((mid, 0, w, h)))
        left_page = crop_margins(im.crop((0, 0, mid, h)))
        sub_images.extend([right_page, left_page])
    elif h > w * 1.5:
        # Dải Manhwa / Webtoon cuộn dài -> Cắt thành các khung tỉ lệ màn hình e-ink
        target_h = int(w * 1.55)
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
        <dc:creator>Manga Scanlator</dc:creator>
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

def generate_otruyen_epub(api_data_url, chapter_title):
    res = requests.get(api_data_url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    payload = res.json()
    item = payload.get('data', {}).get('item', {})
    domain_cdn = item.get('domain_cdn', 'https://sv1.otruyencdn.com').rstrip('/')
    chapter_path = item.get('chapter_path', '').strip('/')
    images = item.get('chapter_image', [])

    if not images:
        raise ValueError("Không tìm thấy hình ảnh nào trong chương này trên OTruyen.")

    processed_images = []
    print(f"[*] [OTruyen] Đang tải và tối ưu {len(images)} trang ảnh cho: {chapter_title}...")
    for idx, img_info in enumerate(images):
        fname = img_info.get('image_file') if isinstance(img_info, dict) else str(img_info)
        if not fname:
            continue
        img_url = f"{domain_cdn}/{chapter_path}/{fname}"
        try:
            r = requests.get(img_url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                buffers = optimize_and_split_image(r.content)
                processed_images.extend(buffers)
        except Exception as e:
            print(f"[!] Lỗi tải trang {idx+1}: {e}")

    if not processed_images:
        raise ValueError("Không tải được ảnh hợp lệ nào từ chương OTruyen này.")

    safe_title = chapter_title or "Chapter"
    epub_bytes = build_epub_file("otruyen", safe_title, processed_images)
    return safe_title, epub_bytes

def generate_mangadex_epub(chapter_id, chapter_title):
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
    return safe_title, epub_bytes

def generate_notice_epub(manga_title):
    safe_title = f"Thong_bao_{manga_title[:20]}"
    esc_title = html.escape(manga_title)
    
    img = Image.new('L', (480, 800), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([15, 80, 465, 720], outline=0, width=4)
    draw.text((40, 130), "THONG BAO BAN QUYEN", fill=0)
    draw.line([40, 160, 440, 160], fill=0, width=2)
    draw.text((40, 190), "Bo truyen:", fill=0)
    draw.text((40, 220), f"{manga_title[:35]}", fill=0)
    draw.text((40, 280), "Chuong tren MangaDex da bi go", fill=0)
    draw.text((40, 315), "theo yeu cau ban quyen (DMCA).", fill=0)
    draw.text((40, 380), "GIAI PHAP:", fill=0)
    draw.text((40, 415), "- Bam nut 'Quay lai' tren may Xteink", fill=0)
    draw.text((40, 450), "- Chon muc [Kho OTruyen] o Menu chinh", fill=0)
    draw.text((40, 485), "- Tim bo nay de doc tron bo day du", fill=0)
    draw.text((40, 520), "  ma khong lo bi khoa ban quyen!", fill=0)
    
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    img_bytes = buf.getvalue()
    epub_bytes = build_epub_file("dmca_notice", esc_title, [img_bytes])
    return safe_title, epub_bytes

# ----------------- Data Fetching Functions -----------------

def fetch_otruyen_list(list_type, page=1):
    cache_key = f"otruyen_list_{list_type}_{page}"
    now = time.time()
    if cache_key in CACHE:
        ts, cached = CACHE[cache_key]
        if now - ts < 600:
            return cached

    url = f"{OTRUYEN_API}/danh-sach/{list_type}?page={page}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    data = res.json().get('data', {})
    items = data.get('items', [])
    pagination = data.get('params', {}).get('pagination', {})
    result = (items, pagination)
    CACHE[cache_key] = (now, result)
    return result

def fetch_otruyen_search(keyword, page=1):
    url = f"{OTRUYEN_API}/tim-kiem?keyword={urllib.parse.quote(keyword)}&page={page}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    data = res.json().get('data', {})
    return data.get('items', [])

def fetch_otruyen_detail(slug):
    cache_key = f"otruyen_detail_{slug}"
    now = time.time()
    if cache_key in CACHE:
        ts, cached = CACHE[cache_key]
        if now - ts < 900:
            return cached

    url = f"{OTRUYEN_API}/truyen-tranh/{slug}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    item = res.json().get('data', {}).get('item', {})
    CACHE[cache_key] = (now, item)
    return item

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
    cache_key = f"mangadex_ch_{manga_id}"
    if cache_key in CACHE:
        ts, cached = CACHE[cache_key]
        if now - ts < 900:
            return cached

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
    CACHE[cache_key] = (now, chapters)
    return chapters

# ----------------- OPDS Feed Builders -----------------

def build_root_feed(base_url):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:root</id>
  <title>Kho Truyện Tranh (E-Ink Bridge)</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <author><name>Xteink Cloud Bridge</name></author>
  <link rel="self" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>

  <entry>
    <title>🔍 Tìm Kiếm Truyện Nhanh (Gõ Tên / Gợi Ý)</title>
    <id>urn:xteink:search_menu</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tìm kiếm nhanh trên cả OTruyen và MangaDex hoặc chọn danh mục gợi ý</summary>
    <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>
    <link rel="subsection" href="{base_url}/opds/search_shortcuts" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  </entry>

  <entry>
    <title>📚 [OTruyen] Mới Cập Nhật (Đầy Đủ, Không DMCA)</title>
    <id>urn:xteink:otruyen:truyenmoi</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Truyện tranh tiếng Việt mới nhất, không bị hạn chế bản quyền</summary>
    <link rel="subsection" href="{base_url}/opds/otruyen/list?type=truyen-moi" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🔥 [OTruyen] Đang Phát Hành / Hot</title>
    <id>urn:xteink:otruyen:dangphathanh</id>
    <summary>Các bộ truyện tranh đang ra chap mới cực hot trên OTruyen</summary>
    <link rel="subsection" href="{base_url}/opds/otruyen/list?type=dang-phat-hanh" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🏆 [OTruyen] Trọn Bộ Hoàn Thành (Solo Leveling...)</title>
    <id>urn:xteink:otruyen:hoanthanh</id>
    <summary>Các bộ truyện đã hoàn thành trọn bộ (Tôi Thăng Cấp Một Mình full 239 chap...)</summary>
    <link rel="subsection" href="{base_url}/opds/otruyen/list?type=hoan-thanh" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🌐 [MangaDex] Truyện Quốc Tế Xem Nhiều</title>
    <id>urn:xteink:mangadex:popular</id>
    <summary>Kho truyện MangaDex quốc tế có bản dịch tiếng Việt</summary>
    <link rel="subsection" href="{base_url}/opds/mangadex/list?type=popular" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>⭐ [MangaDex] Đánh Giá Cao Nhất</title>
    <id>urn:xteink:mangadex:rating</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Manga tiếng Việt có điểm đánh giá cao nhất trên MangaDex</summary>
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

# ----------------- OTruyen Feeds -----------------

def build_otruyen_list_feed(base_url, feed_title, list_type, items, page=1, total_pages=1):
    entries = []
    for item in items:
        name = item.get('name', 'Truyện')
        slug = item.get('slug', '')
        thumb_url = get_otruyen_thumb(item.get('thumb_url', ''))
        status = item.get('status', '')
        summary = f"Trạng thái: {status}" if status else "Truyện tranh OTruyen tiếng Việt"
        
        cover_xml = ""
        if thumb_url:
            cover_xml = f'''<link rel="http://opds-spec.org/image" href="{thumb_url}" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{thumb_url}" type="image/jpeg"/>'''

        safe_slug = urllib.parse.quote(slug)
        safe_title = urllib.parse.quote(name)
        entries.append(f'''  <entry>
    <title>{xml_escape(name)}</title>
    <id>urn:otruyen:manga:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{xml_escape(summary)}</summary>
    {cover_xml}
    <link rel="subsection" href="{base_url}/opds/otruyen/manga?slug={safe_slug}&amp;title={safe_title}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    if page < total_pages:
        next_page = page + 1
        next_url = f"{base_url}/opds/otruyen/list?type={list_type}&amp;page={next_page}"
        entries.append(f'''  <entry>
    <title>⏩ Trang Tiếp Theo (Trang {next_page} / {total_pages})</title>
    <id>urn:otruyen:page:{list_type}:{next_page}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Xem thêm các bộ truyện ở trang tiếp theo</summary>
    <link rel="subsection" href="{next_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:otruyen:list:{list_type}:{page}</id>
  <title>{xml_escape(feed_title)} (Trang {page})</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

def create_otruyen_chapter_entry(base_url, manga_title, ch):
    ch_name = ch.get('chapter_name', '1')
    ch_title = ch.get('chapter_title', '')
    api_data_url = ch.get('chapter_api_data', '')
    
    full_title = f"Chương {ch_name}" + (f": {ch_title}" if ch_title else "")
    esc_title = xml_escape(full_title)
    
    safe_title = urllib.parse.quote(f"{manga_title} - Chap {ch_name}")
    safe_api = urllib.parse.quote(api_data_url)
    download_url = f"{base_url}/download_otruyen?api_data={safe_api}&amp;title={safe_title}"

    return f'''  <entry>
    <title>{esc_title}</title>
    <id>urn:otruyen:chapter:{urllib.parse.quote(ch_name)}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Đọc chương {ch_name} nguồn OTruyen</summary>
    <link rel="http://opds-spec.org/acquisition" href="{download_url}" type="application/epub+zip"/>
  </entry>'''

def build_otruyen_chapter_feed(base_url, slug, manga_title, chapters, order='desc', range_param=None, search_q=None):
    entries = []
    safe_slug = urllib.parse.quote(slug)
    safe_title = urllib.parse.quote(manga_title)

    if not chapters:
        entries.append(f'''  <entry>
    <title>Chưa có chương nào sẵn sàng</title>
    <id>urn:otruyen:empty:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bộ truyện này hiện chưa có tập truyện nào được cập nhật trên OTruyen.</summary>
  </entry>''')
        return format_feed(base_url, f"otruyen:{slug}", manga_title, entries)

    sorted_asc = sorted(chapters, key=lambda c: parse_num(c.get('chapter_name', '0')))
    sorted_desc = sorted(chapters, key=lambda c: parse_num(c.get('chapter_name', '0')), reverse=True)
    first_ch = sorted_asc[0]
    latest_ch = sorted_desc[0]

    first_num = first_ch.get('chapter_name', '1')
    latest_num = latest_ch.get('chapter_name', 'Mới')

    # 1. Chế độ tìm số chương
    if search_q:
        q_lower = search_q.strip().lower()
        matched = []
        for ch in sorted_desc:
            c_name = str(ch.get('chapter_name', '')).lower()
            c_title = str(ch.get('chapter_title', '')).lower()
            if q_lower == c_name or f"chương {q_lower}" in c_title or q_lower in c_title:
                matched.append(ch)

        back_url = f"{base_url}/opds/otruyen/manga?slug={safe_slug}&amp;title={safe_title}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Danh Mục Chương</title>
    <id>urn:otruyen:back:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Xem toàn bộ danh mục chương</summary>
    <link rel="subsection" href="{back_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        if not matched:
            entries.append(f'''  <entry>
    <title>Không tìm thấy chương nào khớp với "{xml_escape(search_q)}"</title>
    <id>urn:otruyen:notfound:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Vui lòng thử gõ số chương khác hoặc bấm 'Quay Lại'</summary>
  </entry>''')
        else:
            for ch in matched:
                entries.append(create_otruyen_chapter_entry(base_url, manga_title, ch))
        return format_feed(base_url, f"otruyen:{slug}", f"{manga_title} (Tìm số: {search_q})", entries)

    # 2. Chế độ xem theo khoảng chương (Range)
    if range_param:
        try:
            start_idx, end_idx = map(int, range_param.split('-'))
        except:
            start_idx, end_idx = 0, 40

        active_list = sorted_asc if order == 'asc' else sorted_desc
        subset = active_list[start_idx:end_idx]

        back_url = f"{base_url}/opds/otruyen/manga?slug={safe_slug}&amp;title={safe_title}&amp;order={order}"
        entries.append(f'''  <entry>
    <title>« Quay Lại Toàn Bộ Chương</title>
    <id>urn:otruyen:back:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Trở về danh mục phân đoạn chương</summary>
    <link rel="subsection" href="{back_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        for ch in subset:
            entries.append(create_otruyen_chapter_entry(base_url, manga_title, ch))
            
        r_title = f"{manga_title} (Khoảng {start_idx+1} - {min(end_idx, len(active_list))})"
        return format_feed(base_url, f"otruyen:{slug}", r_title, entries)

    # 3. Chế độ mặc định: Phím tắt + Phân đoạn
    first_safe_api = urllib.parse.quote(first_ch.get('chapter_api_data', ''))
    first_download = f"{base_url}/download_otruyen?api_data={first_safe_api}&amp;title={urllib.parse.quote(f'{manga_title} - Chap {first_num}')}"
    entries.append(f'''  <entry>
    <title>⭐ [ĐỌC TỪ ĐẦU] Chương {first_num}</title>
    <id>urn:otruyen:readfirst:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải và đọc ngay từ chương đầu tiên</summary>
    <link rel="http://opds-spec.org/acquisition" href="{first_download}" type="application/epub+zip"/>
  </entry>''')

    latest_safe_api = urllib.parse.quote(latest_ch.get('chapter_api_data', ''))
    latest_download = f"{base_url}/download_otruyen?api_data={latest_safe_api}&amp;title={urllib.parse.quote(f'{manga_title} - Chap {latest_num}')}"
    entries.append(f'''  <entry>
    <title>🔥 [ĐỌC MỚI NHẤT] Chương {latest_num}</title>
    <id>urn:otruyen:readlatest:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tải và đọc ngay chương mới nhất</summary>
    <link rel="http://opds-spec.org/acquisition" href="{latest_download}" type="application/epub+zip"/>
  </entry>''')

    next_order = 'asc' if order == 'desc' else 'desc'
    order_label = "Từ Chương 1 -> Mới nhất" if order == 'desc' else "Mới nhất -> Cũ nhất"
    order_toggle_url = f"{base_url}/opds/otruyen/manga?slug={safe_slug}&amp;order={next_order}&amp;title={safe_title}"
    entries.append(f'''  <entry>
    <title>🔄 Đổi Thứ Tự: Xem [{order_label}]</title>
    <id>urn:otruyen:order:{safe_slug}:{next_order}</id>
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
            range_url = f"{base_url}/opds/otruyen/manga?slug={safe_slug}&amp;range={i}-{i+chunk_size}&amp;order={order}&amp;title={safe_title}"
            entries.append(f'''  <entry>
    <title>📁 Khoảng: Chương {c_start} -> {c_end} ({len(chunk)} tập)</title>
    <id>urn:otruyen:range:{safe_slug}:{i}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Mở danh sách gồm {len(chunk)} chương từ {c_start} đến {c_end}</summary>
    <link rel="subsection" href="{range_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    else:
        for ch in active_list:
            entries.append(create_otruyen_chapter_entry(base_url, manga_title, ch))

    return format_feed(base_url, f"otruyen:{slug}", manga_title, entries)

# ----------------- MangaDex Feeds -----------------

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
    
    # Smart Fallback nếu MangaDex bị gỡ chương (DMCA)
    if not all_chapters:
        # Thử tìm trên OTruyen tự động
        otruyen_match = None
        try:
            ot_items = fetch_otruyen_search(manga_title)
            if ot_items:
                otruyen_match = ot_items[0]
        except Exception:
            pass

        if otruyen_match:
            ot_slug = otruyen_match.get('slug', '')
            ot_name = otruyen_match.get('name', manga_title)
            ot_url = f"{base_url}/opds/otruyen/manga?slug={urllib.parse.quote(ot_slug)}&amp;title={urllib.parse.quote(ot_name)}"
            entries.append(f'''  <entry>
    <title>✨ [TỰ ĐỘNG CHUYỂN NGUỒN] Đọc Trọn Bộ Trên OTruyen</title>
    <id>urn:fallback:otruyen:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bộ truyện "{xml_escape(manga_title)}" bị khóa bản quyền trên MangaDex nhưng ĐÃ ĐƯỢC TÌM THẤY trên OTruyen. Bấm vào đây để mở trọn bộ đọc ngay!</summary>
    <link rel="subsection" href="{ot_url}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

        notice_url = f"{base_url}/download_notice?title={safe_manga_title}"
        entries.append(f'''  <entry>
    <title>⚠️ Đã bị gỡ trên MangaDex do bản quyền DMCA</title>
    <id>urn:mangadex:empty:{manga_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Bấm vào đây để tải thông báo chi tiết và hướng dẫn chuyển sang nguồn OTruyen.</summary>
    <link rel="http://opds-spec.org/acquisition" href="{notice_url}" type="application/epub+zip"/>
  </entry>''')
        return format_feed(base_url, f"mangadex:{manga_id}", manga_title, entries)

    sorted_asc = sorted(all_chapters, key=lambda c: parse_num(c.get('attributes', {}).get('chapter', '0')))
    sorted_desc = sorted(all_chapters, key=lambda c: parse_num(c.get('attributes', {}).get('chapter', '0')), reverse=True)
    first_ch = sorted_asc[0]
    latest_ch = sorted_desc[0]
    
    first_num = first_ch.get('attributes', {}).get('chapter') or '1'
    latest_num = latest_ch.get('attributes', {}).get('chapter') or 'Mới'

    # 1. Chế độ lọc theo tìm kiếm số chương
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

    # 2. Chế độ xem theo khoảng chương (Range)
    if range_param:
        try:
            start_idx, end_idx = map(int, range_param.split('-'))
        except:
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

    # 3. Chế độ mặc định
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

# ----------------- Unified Search Feed -----------------

def build_unified_search_feed(base_url, query):
    entries = []
    
    # 1. Search OTruyen (Ưu tiên số 1 vì không bao giờ bị DMCA)
    try:
        ot_items = fetch_otruyen_search(query)
        for item in ot_items[:20]:
            name = item.get('name', 'Truyện')
            slug = item.get('slug', '')
            thumb = get_otruyen_thumb(item.get('thumb_url', ''))
            safe_slug = urllib.parse.quote(slug)
            safe_title = urllib.parse.quote(name)
            
            cover_xml = ""
            if thumb:
                cover_xml = f'''<link rel="http://opds-spec.org/image" href="{thumb}" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{thumb}" type="image/jpeg"/>'''

            entries.append(f'''  <entry>
    <title>[OTruyen] {xml_escape(name)}</title>
    <id>urn:search:otruyen:{safe_slug}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Nguồn OTruyen (Đầy đủ, không sợ bản quyền)</summary>
    {cover_xml}
    <link rel="subsection" href="{base_url}/opds/otruyen/manga?slug={safe_slug}&amp;title={safe_title}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    except Exception as e:
        print(f"[!] OTruyen search error: {e}")

    # 2. Search MangaDex
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

def format_feed(base_url, feed_id, feed_title, entries):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:{feed_id}</id>
  <title>{xml_escape(feed_title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

# ----------------- HTTP Handler -----------------

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

            # 1. Root & Search
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

            # 2. OTruyen Routing
            elif path == '/opds/otruyen/list':
                list_type = qs.get('type', ['truyen-moi'])[0]
                page = int(qs.get('page', ['1'])[0])
                title_map = {
                    'truyen-moi': 'OTruyen - Mới Cập Nhật',
                    'dang-phat-hanh': 'OTruyen - Đang Phát Hành',
                    'hoan-thanh': 'OTruyen - Trọn Bộ Hoàn Thành'
                }
                feed_title = title_map.get(list_type, 'OTruyen')
                items, pagination = fetch_otruyen_list(list_type, page=page)
                total_pages = pagination.get('totalPages', 1)
                xml_data = build_otruyen_list_feed(base_url, feed_title, list_type, items, page=page, total_pages=total_pages)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/otruyen/manga':
                slug = qs.get('slug', [''])[0]
                if not slug:
                    self.send_error(400, "Missing slug")
                    return
                manga_title = qs.get('title', [''])[0]
                item = fetch_otruyen_detail(slug)
                if not manga_title:
                    manga_title = item.get('name', 'Truyện')

                chapters = []
                for srv in item.get('chapters', []):
                    s_data = srv.get('server_data', [])
                    if s_data:
                        chapters = s_data
                        break

                order = qs.get('order', ['desc'])[0]
                range_param = qs.get('range', [''])[0]
                search_q = qs.get('q', [''])[0].strip()

                xml_data = build_otruyen_chapter_feed(base_url, slug, manga_title, chapters, order=order, range_param=range_param, search_q=search_q)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            # 3. MangaDex Routing (kèm tương thích ngược)
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
                ot_slug = qs.get('slug', [''])[0]
                
                # Nếu truyền slug (tương thích ngược) -> chuyển sang otruyen
                if ot_slug:
                    item = fetch_otruyen_detail(ot_slug)
                    manga_title = qs.get('title', [''])[0] or item.get('name', 'Truyện')
                    chapters = []
                    for srv in item.get('chapters', []):
                        if srv.get('server_data'):
                            chapters = srv.get('server_data')
                            break
                    xml_data = build_otruyen_chapter_feed(base_url, ot_slug, manga_title, chapters)
                else:
                    if not manga_id:
                        self.send_error(400, "Missing manga id")
                        return
                    manga_title = qs.get('title', [''])[0]
                    if not manga_title:
                        try:
                            manga_title = fetch_mangadex_detail(manga_id)
                        except:
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
            elif path == '/download_otruyen':
                api_data = qs.get('api_data', [''])[0]
                title = qs.get('title', ['Manga_Chapter'])[0]
                if not api_data:
                    self.send_error(400, "Missing api_data")
                    return
                final_title, epub_bytes = generate_otruyen_epub(api_data, title)
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
                final_title, epub_bytes = generate_notice_epub(title)
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
    print(f"[*] Xteink Multi-Source Manga Server (OTruyen + MangaDex) running on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
