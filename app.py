import http.server
import urllib.parse
import json
import os
import io
import zipfile
import html
import sys
import traceback
import requests
from PIL import Image, ImageOps

MANGADEX_API = "https://api.mangadex.org"
COVER_BASE_URL = "https://uploads.mangadex.org/covers"
HEADERS = {
    "User-Agent": "XteinkMangaOPDS/2.0 (Xteink X4 E-Reader Bridge; https://github.com/hieunguyen1666/xteink-manga)",
    "Accept": "application/json"
}

def xml_escape(text):
    return html.escape(text or '')

def get_manga_title(attrs):
    titles = attrs.get('title', {})
    if 'vi' in titles:
        return titles['vi']
    # Check alternative titles for Vietnamese
    for alt in attrs.get('altTitles', []):
        if 'vi' in alt:
            return alt['vi']
    if 'en' in titles:
        return titles['en']
    for v in titles.values():
        return v
    return "Manga"

def get_cover_filename(relationships):
    for rel in relationships:
        if rel.get('type') == 'cover_art':
            return rel.get('attributes', {}).get('fileName')
    return None

def build_root_feed(base_url):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:mangadex:root</id>
  <title>Manga Tiếng Việt (MangaDex)</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <author><name>Xteink Cloud Bridge</name></author>
  <link rel="self" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="search" href="{base_url}/opds/search?q={{searchTerms}}" type="application/atom+xml"/>

  <entry>
    <title>🔥 Truyện Tiếng Việt Xem Nhiều / Hot</title>
    <id>urn:xteink:mangadex:popular</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Những bộ manga tiếng Việt được theo dõi nhiều nhất</summary>
    <link rel="subsection" href="{base_url}/opds/list?type=popular" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>⭐ Đánh Giá Cao Nhất</title>
    <id>urn:xteink:mangadex:rating</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Manga tiếng Việt có điểm đánh giá cao nhất</summary>
    <link rel="subsection" href="{base_url}/opds/list?type=rating" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🆕 Mới Cập Nhật Chương Mới</title>
    <id>urn:xteink:mangadex:latest</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Các bộ truyện vừa có chapter tiếng Việt mới hôm nay</summary>
    <link rel="subsection" href="{base_url}/opds/list?type=latest" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>
</feed>'''

def build_manga_list_feed(base_url, feed_title, feed_id, manga_list):
    entries = []
    for item in manga_list:
        m_id = item['id']
        attrs = item.get('attributes', {})
        title = xml_escape(get_manga_title(attrs))
        
        # Description
        desc_dict = attrs.get('description', {})
        desc_text = desc_dict.get('vi') or desc_dict.get('en') or 'Truyện tranh MangaDex tiếng Việt'
        desc_clean = xml_escape(desc_text[:200] + ('...' if len(desc_text) > 200 else ''))
        
        # Cover image
        cover_file = get_cover_filename(item.get('relationships', []))
        cover_link_xml = ""
        if cover_file:
            cover_url = f"{COVER_BASE_URL}/{m_id}/{cover_file}.256.jpg"
            cover_link_xml = f'''<link rel="http://opds-spec.org/image" href="{cover_url}" type="image/jpeg"/>
    <link rel="http://opds-spec.org/image/thumbnail" href="{cover_url}" type="image/jpeg"/>'''

        entries.append(f'''  <entry>
    <title>{title}</title>
    <id>urn:mangadex:manga:{m_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{desc_clean}</summary>
    {cover_link_xml}
    <link rel="subsection" href="{base_url}/opds/manga?id={m_id}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')

    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:mangadex:{feed_id}</id>
  <title>{xml_escape(feed_title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="self" href="{base_url}/opds/list?type={feed_id}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

def build_chapter_feed(base_url, manga_id, manga_title, chapters):
    entries = []
    for ch in chapters:
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

        entries.append(f'''  <entry>
    <title>{esc_title}</title>
    <id>urn:mangadex:chapter:{ch_id}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{summary}</summary>
    <link rel="http://opds-spec.org/acquisition" href="{download_url}" type="application/epub+zip"/>
  </entry>''')

    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:mangadex:manga:{manga_id}</id>
  <title>{xml_escape(manga_title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{chr(10).join(entries)}
</feed>'''

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
    data = res.json()
    return data.get('data', [])

def fetch_manga_detail(manga_id):
    res = requests.get(f"{MANGADEX_API}/manga/{manga_id}", headers=HEADERS, timeout=15)
    res.raise_for_status()
    data = res.json()
    item = data.get('data', {})
    attrs = item.get('attributes', {})
    return get_manga_title(attrs)

def fetch_chapters(manga_id):
    params = {
        "limit": 100,
        "translatedLanguage[]": ["vi"],
        "order[chapter]": "desc",
        "contentRating[]": ["safe", "suggestive", "erotica"],
        "includeFutureUpdates": "0"
    }
    res = requests.get(f"{MANGADEX_API}/manga/{manga_id}/feed", headers=HEADERS, params=params, timeout=15)
    res.raise_for_status()
    data = res.json()
    return data.get('data', [])

def generate_epub(chapter_id, chapter_title):
    # 1. Fetch MangaDex At-Home server for chapter images
    res = requests.get(f"{MANGADEX_API}/at-home/server/{chapter_id}", headers=HEADERS, timeout=15)
    res.raise_for_status()
    srv_data = res.json()
    
    base_url = srv_data.get('baseUrl')
    chapter_info = srv_data.get('chapter', {})
    ch_hash = chapter_info.get('hash')
    # Use dataSaver for lightweight e-ink transfer, fallback to data
    file_list = chapter_info.get('dataSaver') or chapter_info.get('data') or []
    sub_folder = "data-saver" if chapter_info.get('dataSaver') else "data"

    if not file_list:
        raise ValueError("Không tìm thấy hình ảnh nào trong chương này trên MangaDex.")

    # 2. Download and process images
    processed_images = []
    print(f"[*] Đang tải {len(file_list)} trang ảnh cho: {chapter_title}...")
    for idx, fname in enumerate(file_list):
        img_url = f"{base_url}/{sub_folder}/{ch_hash}/{fname}"
        try:
            r = requests.get(img_url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                im = Image.open(io.BytesIO(r.content))
                if im.mode != 'L':
                    im = ImageOps.grayscale(im)
                # Resize for Xteink X4 480x800 e-ink screen
                im.thumbnail((480, 800), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=75, optimize=True)
                processed_images.append(buf.getvalue())
        except Exception as e:
            print(f"[!] Lỗi tải trang {idx+1}: {e}")

    if not processed_images:
        raise ValueError("Không thể tải được hình ảnh hợp lệ nào từ chương này.")

    # 3. Create EPUB in-memory
    safe_title = chapter_title or f"Chapter_{chapter_id}"
    esc_safe_title = html.escape(safe_title)
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
        <dc:identifier id="BookID">urn:mangadex:{chapter_id}</dc:identifier>
        <dc:creator>MangaDex Scanlator</dc:creator>
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
        <meta name="dtb:uid" content="urn:mangadex:{chapter_id}"/>
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

    return safe_title, epub_buf.getvalue()

class MangaOPDSHandler(http.server.BaseHTTPRequestHandler):
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

            if path in ['/', '/opds']:
                xml_data = build_root_feed(base_url)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/list':
                list_type = qs.get('type', ['popular'])[0]
                feed_title = "Truyện Phổ Biến"
                if list_type == "rating":
                    feed_title = "Đánh Giá Cao"
                elif list_type == "latest":
                    feed_title = "Mới Cập Nhật"
                
                manga_list = fetch_mangadex_list(list_type)
                xml_data = build_manga_list_feed(base_url, feed_title, list_type, manga_list)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/search':
                q = qs.get('q', [''])[0].strip()
                feed_title = f"Tìm kiếm: {q}" if q else "Tìm kiếm"
                manga_list = fetch_mangadex_list("popular", query=q) if q else []
                xml_data = build_manga_list_feed(base_url, feed_title, "search", manga_list)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/manga':
                manga_id = qs.get('id', [''])[0]
                if not manga_id:
                    self.send_error(400, "Missing manga id")
                    return
                manga_title = fetch_manga_detail(manga_id)
                chapters = fetch_chapters(manga_id)
                xml_data = build_chapter_feed(base_url, manga_id, manga_title, chapters)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/download':
                chapter_id = qs.get('chapter_id', [''])[0]
                title = qs.get('title', [f'Manga_{chapter_id}'])[0]
                if not chapter_id:
                    self.send_error(400, "Missing chapter_id")
                    return
                
                final_title, epub_bytes = generate_epub(chapter_id, title)
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
  <title>Lỗi MangaDex</title>
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
    httpd = http.server.ThreadingHTTPServer(server_address, MangaOPDSHandler)
    print(f"[*] Xteink MangaDex Server running on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
