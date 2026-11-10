import http.server
import urllib.parse
import re
import os
import io
import zipfile
import html
import sys
import traceback
import requests
from PIL import Image, ImageOps

BASE_TRUYENQQ = 'https://truyenqq.com.vn'

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'vi,en-US;q=0.9,en;q=0.8',
    'Referer': 'https://truyenqq.com.vn/'
})

def fetch_html(url):
    if not url.startswith('http'):
        url = urllib.parse.urljoin(BASE_TRUYENQQ, url)
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    return resp.text

def fetch_bytes(url):
    if not url.startswith('http'):
        url = urllib.parse.urljoin(BASE_TRUYENQQ, url)
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.content

def xml_escape(text):
    return html.escape(text or '')

def build_root_feed(base_url):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:manga:root</id>
  <title>Kho Truyện Tranh TruyenQQ (CrossPoint E-Ink)</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <author><name>CrossPoint Manga Bridge</name></author>
  <link rel="self" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <link rel="search" href="{base_url}/opds/search?s={{searchTerms}}" type="application/atom+xml" title="Tìm kiếm truyện tranh"/>

  <entry>
    <title>🔥 Truyện Phổ Biến / Hot</title>
    <id>urn:xteink:manga:hot</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Tuyển tập các bộ truyện tranh hot nhất</summary>
    <link rel="subsection" href="{base_url}/opds/list?path=%2Ftruyen-hot%2F" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>

  <entry>
    <title>🆕 Truyện Mới Cập Nhật</title>
    <id>urn:xteink:manga:new</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>Các chương truyện vừa được cập nhật hôm nay</summary>
    <link rel="subsection" href="{base_url}/opds/list?path=%2Ftruyen-moi" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>
</feed>'''

def build_manga_list_feed(base_url, title, items):
    entries = []
    for item in items:
        manga_title = xml_escape(item['title'])
        manga_path = urllib.parse.quote(item['path'])
        entries.append(f'''  <entry>
    <title>{manga_title}</title>
    <id>urn:xteink:manga:{manga_path}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <summary>{manga_title}</summary>
    <link rel="subsection" href="{base_url}/opds/manga?path={manga_path}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>''')
    entries_xml = "\n".join(entries)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:manga:list</id>
  <title>{xml_escape(title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{entries_xml}
</feed>'''

def build_chapter_feed(base_url, manga_title, manga_path, chapters):
    entries = []
    for ch in chapters:
        ch_title = xml_escape(ch['title'])
        ch_path = urllib.parse.quote(ch['path'])
        download_url = f"{base_url}/download?url={ch_path}"
        entries.append(f'''  <entry>
    <title>{ch_title}</title>
    <id>urn:xteink:chapter:{ch_path}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <author><name>TruyenQQ</name></author>
    <summary>{xml_escape(manga_title)} - {ch_title}</summary>
    <link rel="http://opds-spec.org/acquisition" href="{download_url}" type="application/epub+zip"/>
  </entry>''')
    entries_xml = "\n".join(entries)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:xteink:manga:{urllib.parse.quote(manga_path)}</id>
  <title>{xml_escape(manga_title)}</title>
  <updated>2026-11-10T00:00:00Z</updated>
  <link rel="start" href="{base_url}/opds" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
{entries_xml}
</feed>'''

def generate_epub(chapter_url):
    page_html = fetch_html(chapter_url)
    
    title_match = re.search(r'<title>(.*?)</title>', page_html)
    raw_title = title_match.group(1) if title_match else "Manga Chapter"
    raw_title = html.unescape(raw_title)
    raw_title = re.sub(r'\s*-\s*TruyenQQ.*', '', raw_title, flags=re.IGNORECASE).strip()
    raw_title = re.sub(r'[\\/*?:"<>|]', '', raw_title) or "TruyenQQ_Chapter"

    img_urls = re.findall(r'<img[^>]+(?:data-src|data-original|data-cdn)=["\']([^"\']+)["\']', page_html)
    filtered_imgs = [u for u in img_urls if any(ext in u.lower() for ext in ['.webp', '.jpg', '.jpeg', '.png']) and 'logo' not in u and 'icon' not in u]
    if not filtered_imgs:
        all_imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', page_html)
        filtered_imgs = [u for u in all_imgs if 'chapters' in u]
    
    seen = set()
    final_imgs = []
    for u in filtered_imgs:
        if u not in seen:
            seen.add(u)
            final_imgs.append(u)

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, 'w') as zf:
        zf.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        
        container_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>'''
        zf.writestr('META-INF/container.xml', container_xml.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)
        
        css = '''@page { margin: 0; padding: 0; }
body { margin: 0; padding: 0; text-align: center; background-color: #ffffff; }
div.page { width: 100%; height: 100vh; margin: 0; padding: 0; text-align: center; }
img { max-width: 100%; max-height: 100%; width: auto; height: auto; margin: 0 auto; display: block; }'''
        zf.writestr('OEBPS/style.css', css.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)

        manifest_items = [
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="css" href="style.css" media-type="text/css"/>'
        ]
        spine_items = []

        for idx, img_url in enumerate(final_imgs, 1):
            if not img_url.startswith('http'):
                img_url = urllib.parse.urljoin(chapter_url, img_url)

            try:
                img_data = fetch_bytes(img_url)
                with Image.open(io.BytesIO(img_data)) as img:
                    if img.mode != 'L':
                        img = img.convert('L')
                    img = ImageOps.autocontrast(img, cutoff=0.5)
                    img.thumbnail((480, 800), Image.Resampling.LANCZOS)
                    
                    out_io = io.BytesIO()
                    img.save(out_io, format='JPEG', quality=85, optimize=True)
                    img_bytes = out_io.getvalue()
            except Exception as e:
                continue

            img_name = f"page_{idx:03d}.jpg"
            zf.writestr(f"OEBPS/images/{img_name}", img_bytes, compress_type=zipfile.ZIP_DEFLATED)

            html_content = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Trang {idx}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><div class="page"><img src="images/{img_name}" alt="Page {idx}"/></div></body>
</html>'''
            page_name = f"page_{idx:03d}.xhtml"
            zf.writestr(f"OEBPS/{page_name}", html_content.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)
            
            manifest_items.append(f'<item id="img_{idx}" href="images/{img_name}" media-type="image/jpeg"/>')
            manifest_items.append(f'<item id="page_{idx}" href="{page_name}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="page_{idx}"/>')

        opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="2.0">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>{xml_escape(raw_title)}</dc:title>
        <dc:language>vi</dc:language>
        <dc:identifier id="BookId">urn:uuid:truyenqq_{idx}</dc:identifier>
        <meta name="cover" content="img_1"/>
    </metadata>
    <manifest>
        {"".join(manifest_items)}
    </manifest>
    <spine toc="ncx">
        {"".join(spine_items)}
    </spine>
</package>'''
        zf.writestr('OEBPS/content.opf', opf.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)
        
        ncx = f'''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
    <head><meta name="dtb:uid" content="urn:uuid:truyenqq_{idx}"/></head>
    <docTitle><text>{xml_escape(raw_title)}</text></docTitle>
    <navMap>
        <navPoint id="navpoint-1" playOrder="1">
            <navLabel><text>{xml_escape(raw_title)}</text></navLabel>
            <content src="page_001.xhtml"/>
        </navPoint>
    </navMap>
</ncx>'''
        zf.writestr('OEBPS/toc.ncx', ncx.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)

    mem_zip.seek(0)
    return raw_title, mem_zip.getvalue()

class MangaOPDSHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stdout.write(f"[{self.log_date_time_string()}] {format % args}\n")
        sys.stdout.flush()

    def get_base_url(self):
        proto = self.headers.get('X-Forwarded-Proto') or 'https'
        host = self.headers.get('Host') or '127.0.0.1:8000'
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
                self.send_header('Content-Length', str(len(xml_data.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/list':
                category_path = qs.get('path', ['/truyen-hot/'])[0]
                cat_html = fetch_html(category_path)
                
                tags = re.findall(r'<a\s+[^>]*href=[\'"]([^\'"]*truyen-tranh/[^\'"]+|/[a-z0-9-]+)[\'"][^>]*>(.*?)</a>', cat_html, re.DOTALL)
                seen = set()
                items = []
                for href, inner in tags:
                    if href in ['/dang-nhap', '/contact', '/truyen-hot/', '/truyen-moi', '/the-loai/truyen-mau'] or 'the-loai' in href:
                        continue
                    t = html.unescape(re.sub(r'<[^>]+>', ' ', inner).strip())
                    if t and len(t) > 3 and href not in seen and not href.endswith('/chapter-1'):
                        seen.add(href)
                        items.append({'title': t, 'path': href})

                title = "Truyện Phổ Biến" if 'hot' in category_path else "Truyện Mới"
                xml_data = build_manga_list_feed(base_url, title, items[:35])
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.send_header('Content-Length', str(len(xml_data.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/search':
                search_query = qs.get('s', [''])[0]
                if not search_query:
                    search_query = 'sat-thu'
                search_url = f"/tim-kiem?s={urllib.parse.quote(search_query)}"
                search_html = fetch_html(search_url)

                tags = re.findall(r'<a\s+[^>]*href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>', search_html, re.DOTALL)
                seen = set()
                items = []
                for href, inner in tags:
                    if href.startswith('/dang-nhap') or 'the-loai' in href or href in ['/', '/contact']:
                        continue
                    t = html.unescape(re.sub(r'<[^>]+>', ' ', inner).strip())
                    if t and len(t) > 3 and href not in seen and not 'chapter' in href:
                        seen.add(href)
                        items.append({'title': t, 'path': href})

                xml_data = build_manga_list_feed(base_url, f"Tìm kiếm: {search_query}", items[:35])
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.send_header('Content-Length', str(len(xml_data.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/opds/manga':
                manga_path = qs.get('path', [''])[0]
                manga_html = fetch_html(manga_path)
                
                title_m = re.search(r'<h1[^>]*>(.*?)</h1>', manga_html)
                manga_title = html.unescape(re.sub(r'<[^>]+>', '', title_m.group(1)).strip()) if title_m else "Manga"
                
                raw_slug = manga_path.strip('/')
                ch_regex = rf'<a\s+[^>]*href=["\']([^"\']*{raw_slug}/chapter-[^"\']+)["\'][^>]*>(.*?)</a>'
                chapters_match = re.findall(ch_regex, manga_html, re.DOTALL)
                if not chapters_match:
                    chapters_match = re.findall(r'<a\s+[^>]*href=["\']([^"\']*/chapter-[^"\']+)["\'][^>]*>(.*?)</a>', manga_html, re.DOTALL)

                seen = set()
                chapters = []
                for ch_url, ch_inner in chapters_match:
                    ch_name = html.unescape(re.sub(r'<[^>]+>', '', ch_inner).strip())
                    if ch_name.lower().startswith("đọc từ đầu"):
                        continue
                    if ch_url not in seen and ch_name:
                        seen.add(ch_url)
                        chapters.append({'title': ch_name, 'path': ch_url})

                xml_data = build_chapter_feed(base_url, manga_title, manga_path, chapters)
                self.send_response(200)
                self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
                self.send_header('Content-Length', str(len(xml_data.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(xml_data.encode('utf-8'))

            elif path == '/download':
                ch_url = qs.get('url', [''])[0]
                if not ch_url:
                    self.send_error(400, "Missing chapter url")
                    return

                print(f"[*] Request download chapter: {ch_url}")
                title, epub_bytes = generate_epub(ch_url)
                print(f"[+] Built EPUB for '{title}', size: {len(epub_bytes)} bytes")

                safe_name = urllib.parse.quote(f"{title}.epub")
                self.send_response(200)
                self.send_header('Content-Type', 'application/epub+zip')
                self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{safe_name}')
                self.send_header('Content-Length', str(len(epub_bytes)))
                self.end_headers()
                self.wfile.write(epub_bytes)

            else:
                self.send_error(404, "Not Found")

        except Exception as e:
            err_msg = traceback.format_exc()
            print(f"[!] Server Error on {self.path}: {err_msg}")
            xml_err = f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Lỗi kết nối TruyenQQ</title>
  <entry>
    <title>Không thể lấy dữ liệu: {xml_escape(str(e))}</title>
    <summary>{xml_escape(err_msg)}</summary>
  </entry>
</feed>'''
            self.send_response(200)
            self.send_header('Content-Type', 'application/atom+xml;charset=utf-8')
            self.send_header('Content-Length', str(len(xml_err.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(xml_err.encode('utf-8'))

def run():
    port = int(os.environ.get('PORT', sys.argv[1] if len(sys.argv) > 1 else 7860))
    server_address = ('0.0.0.0', port)
    httpd = http.server.ThreadingHTTPServer(server_address, MangaOPDSHandler)
    print(f"[*] CrossPoint Manga OPDS Server running on port {port}...")
    httpd.serve_forever()

if __name__ == '__main__':
    run()
