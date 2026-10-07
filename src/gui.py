#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "pyserial"]
# ///
"""Minimal GUI for print_label.py: pick a printer connection and an image
(or type text), see the exact 1-bit label preview, print.

    python3 gui.py            # opens http://127.0.0.1:8765 in the browser
    python3 gui.py --port 9000 --no-browser

Needs only Pillow (like print_label.py); the UI is a local web page served by
the standard library, so it works the same on Linux and Windows.
"""

import argparse
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import print_label as pl

log = logging.getLogger("gui")

HOST = "127.0.0.1"
DEFAULT_GUI_PORT = 8765
MAX_BODY = 50 * 1024 * 1024  # upload size limit
DRAIN_LIMIT = 256 * 1024 * 1024  # oversized bodies up to this size are read and discarded before the 413

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Печать этикетки TSC</title>
<style>
:root{--bg:#f5f6f8;--fg:#1c1f24;--card:#fff;--line:#d5d9e0;--acc:#1f6feb;--ok:#1a7f37;--err:#cf222e}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e6e8eb;--card:#1e2127;--line:#363b44;--acc:#4c8dff;--ok:#3fb950;--err:#ff7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
main{max-width:1200px;margin:0 auto;padding:clamp(8px,2vw,24px);display:grid;gap:clamp(8px,2vw,16px);grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);align-items:start}
@media(max-width:800px){main{grid-template-columns:minmax(0,1fr)}}
@media(max-width:480px){body{font-size:14px}.row{grid-template-columns:1fr}section{padding:10px}}
@media(max-height:600px) and (min-width:801px){section:last-of-type{position:sticky;top:8px}}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
h1{grid-column:1/-1;margin:0;font-size:20px}h2{margin:0 0 10px;font-size:15px}
label{display:block;margin:8px 0 3px;font-size:13px;opacity:.8}
input,select,textarea,button{width:100%;padding:7px 9px;font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
button{cursor:pointer;background:var(--acc);color:#fff;border:0;margin-top:12px;font-weight:600}
button.sec{background:var(--bg);color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
#pvbox{width:100%}
#pv{display:block;margin:0 auto;image-rendering:pixelated;background:#fff;border:1px solid var(--line)}
#info{margin-top:8px;font-size:13px;opacity:.8;text-align:center}
#log{white-space:pre-wrap;font:12px ui-monospace,monospace;margin-top:10px;padding:8px;border-radius:6px;background:var(--bg);min-height:2.5em}
.ok{color:var(--ok)}.err{color:var(--err)}.hide{display:none}#pv.hide{display:none}
#drop{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(31,111,235,.25);border:4px dashed var(--acc);font-size:28px;font-weight:600;z-index:10;pointer-events:none}
#drop.on{display:flex}
textarea{resize:vertical;min-height:5em}
.chk{display:flex;gap:8px;align-items:center;margin-top:10px}.chk input{width:auto}
</style></head><body><div id="drop">Отпустите файл</div><main>
<h1>Печать картинки или текста на TSC-принтере</h1>
<section>
<h2>Принтер</h2>
<label>Подключение</label>
<select id="conn"><option value="tcp">Сеть (TCP)</option><option value="serial">COM / serial</option><option value="win">Windows-принтер (USB)</option></select>
<div id="tcp"><div class="row"><div><label>IP</label><input id="ip" value="__IP__"></div><div><label>Порт</label><input id="port" type="number" value="__PORT__"></div></div></div>
<div id="serial" class="hide"><div class="row"><div><label>Порт (COM3, /dev/ttyUSB0)</label><input id="sport"></div><div><label>Baud</label><input id="baud" type="number" value="9600"></div></div></div>
<div id="win" class="hide"><label>Имя принтера в Windows</label><input id="wname"></div>
<div class="chk"><input id="emu" type="checkbox"><span>Эмулятор (tsc-emulator, режим совместимости)</span></div>
<button class="sec" id="check">Проверить связь</button>
<h2 style="margin-top:18px">Этикетка</h2>
<label>Что печатаем</label>
<select id="mode"><option value="image">Картинка</option><option value="text">Текст</option></select>
<div id="img"><label>Картинка</label><input id="file" type="file" accept="image/*,.prn"></div>
<div id="txt" class="hide"><label>Текст</label><textarea id="text" rows="4"></textarea>
<div class="row"><div><label>Высота шрифта, мм (пусто — авто)</label><input id="fsize" placeholder="авто"></div>
<div><label>Выравнивание</label><select id="align"><option value="center">по центру</option><option value="left">влево</option><option value="right">вправо</option></select></div></div>
<div class="chk"><input id="bold" type="checkbox"><span>Жирный</span></div></div>
<div class="row"><div><label>Размер, мм (40 или 58x40)</label><input id="size" value="40x40"></div>
<div><label>Копий</label><input id="copies" type="number" min="1" value="1"></div></div>
<div class="row"><div><label>DPI</label><select id="dpi"><option value="auto">авто (запрос ~!T)</option><option value="203">203</option><option value="300">300</option></select></div>
<div><label>Растр</label><select id="dither"><option value="auto">авто</option><option value="floyd">Floyd-Steinberg</option><option value="atkinson">Atkinson</option><option value="bayer">Упорядоченный (Bayer 8x8)</option><option value="threshold">порог (чёрно-белое)</option></select></div></div>
</section>
<section>
<h2>Предпросмотр (как напечатается)</h2>
<div id="pvbox"><img id="pv" alt="" class="hide"></div>
<div id="info">Выберите картинку</div>
<button id="print" disabled>Печатать</button>
<div id="log"></div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
let timer=null,current;
const ids=['conn','ip','port','sport','baud','wname','emu','size','copies','dpi','dither','text','fsize','align','bold'];
const isText=()=>$('mode').value==='text';
function opts(){return{conn:$('conn').value,ip:$('ip').value.trim(),port:+$('port').value,serial:$('sport').value.trim(),baud:+$('baud').value,
 win:$('wname').value.trim(),emu:$('emu').checked,size:$('size').value,copies:+$('copies').value,dpi:$('dpi').value,dither:$('dither').value,
 mode:$('mode').value,fsize:$('fsize').value.trim(),align:$('align').value,bold:$('bold').checked}}
function log(t,c){const l=$('log');l.textContent=t;l.className=c||''}
function show(){for(const k of ['tcp','serial','win'])$(k).classList.toggle('hide',$('conn').value!==k);
 $('img').classList.toggle('hide',isText());$('txt').classList.toggle('hide',!isText())}
async function post(path,body){
  const f=isText()?null:current;
  if(path!=='/check'&&(isText()?!$('text').value.trim():!f)){log(isText()?'Введите текст':'Выберите картинку','err');return null}
  const h={'X-Opts':encodeURIComponent(JSON.stringify(opts())),'X-Filename':encodeURIComponent(f?f.name:'')};
  try{return await fetch(path,{method:'POST',headers:h,body:isText()?$('text').value:(f||'')})}
  catch(e){log('Нет связи с сервером: '+e,'err');return null}
}
async function preview(){
  if(isText()?!$('text').value.trim():!current){
    if(isText()){$('pv').classList.add('hide');$('print').disabled=true;$('info').textContent='Введите текст'}
    return}
  const r=await post('/preview');
  if(!r)return;
  if(!r.ok){$('pv').classList.add('hide');$('print').disabled=true;$('info').textContent='';log(await r.text(),'err');return}
  const j=await r.json();
  if(j.png){const pv=$('pv');pv.onload=fit;pv.src='data:image/png;base64,'+j.png;pv.classList.remove('hide')}else{$('pv').classList.add('hide')}
  $('info').textContent=j.info;$('print').disabled=false;log('');
}
// fit the preview into its box (width and viewport height) without scrollbars:
// enlarge by a whole number of device pixels per dot (sharp), shrink smoothly if it doesn't fit
function fit(){
  const pv=$('pv'),w=pv.naturalWidth,h=pv.naturalHeight;if(!w||!h)return;
  const dpr=window.devicePixelRatio||1;
  const availW=($('pvbox').clientWidth-2)*dpr,availH=Math.max(160,window.innerHeight*0.6)*dpr;
  const s=Math.min(availW/w,availH/h);
  const k=s>=1?Math.floor(s):s;
  pv.style.imageRendering=s>=1?'pixelated':'auto';
  // +2: the 1px border is inside the box (border-box), so the image itself gets exactly w*k device pixels
  pv.style.width=(w*k/dpr+2)+'px';pv.style.height=(h*k/dpr+2)+'px';
}
window.addEventListener('resize',fit);
function later(){clearTimeout(timer);timer=setTimeout(preview,250)}
$('conn').onchange=()=>{show();later()};
for(const i of ids)$(i).oninput=later;
// a dropped/picked file always means "print an image": leave text mode, otherwise preview() ignores the file
function setFile(f){if(!f)return;current=f;console.debug('file',f.name,f.type,f.size);
  if(isText()){$('mode').value='image';show()}
  preview()}
// cancelling the file dialog yields an empty list: keep the previous file and preview
$('file').onchange=()=>setFile($('file').files[0]);
$('mode').onchange=()=>{show();preview()};
let dc=0;
document.addEventListener('dragenter',e=>{e.preventDefault();dc++;$('drop').classList.add('on')});
document.addEventListener('dragleave',e=>{e.preventDefault();dc=Math.max(0,dc-1);if(!dc)$('drop').classList.remove('on')});
document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('drop',e=>{e.preventDefault();dc=0;$('drop').classList.remove('on');
  const dt=e.dataTransfer,it=dt&&[...(dt.items||[])].find(i=>i.kind==='file');
  const f=(dt&&dt.files[0])||(it&&it.getAsFile());
  console.debug('drop',dt&&[...dt.types],f);
  if(f)setFile(f);else log('Браузер не получил файл при перетаскивании — вставьте картинку через Ctrl+V или выберите кнопкой','err')});
// fallback when the file manager gives the browser no drag data (empty types): paste an image with Ctrl+V
document.addEventListener('paste',e=>{
  const f=[...((e.clipboardData&&e.clipboardData.files)||[])][0];
  console.debug('paste',e.clipboardData&&[...e.clipboardData.types],f);
  if(f&&!/^(TEXTAREA|INPUT)$/.test(document.activeElement.tagName)||f&&document.activeElement.type==='file'){e.preventDefault();setFile(f)}});
$('check').onclick=async()=>{log('Проверяю…');const r=await post('/check');if(!r)return;log(await r.text(),r.ok?'ok':'err')};
$('print').onclick=async()=>{
  $('print').disabled=true;log('Отправляю…');
  try{const r=await post('/print');if(r)log(await r.text(),r.ok?'ok':'err')}
  catch(e){log('Нет связи с сервером: '+e,'err')}
  finally{$('print').disabled=false}};
show();
</script></body></html>"""


def parse_opts(raw: str) -> dict:
    from urllib.parse import unquote

    return json.loads(unquote(raw))


def make_target(o: dict) -> pl.Target:
    conn = o.get("conn", "tcp")
    if conn == "serial":
        if not o.get("serial"):
            raise ValueError("укажите COM-порт")
        return pl.Target(serial_port=o["serial"], baud=o.get("baud") or pl.DEFAULT_BAUD)
    if conn == "win":
        if not o.get("win"):
            raise ValueError("укажите имя Windows-принтера")
        return pl.Target(win_printer=o["win"])
    return pl.Target(ip=o.get("ip") or pl.PRINTER_IP, port=o.get("port") or pl.PRINTER_PORT)


def parse_font_mm(text: str) -> float | None:
    """'' / 'авто' -> None (auto-fit); '6' or '6,5' -> mm."""
    cleaned = text.lower().replace("мм", "").replace("mm", "").replace(",", ".").strip()
    if cleaned in ("", "auto", "авто"):
        return None
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"высота шрифта: число в мм или пусто, а не {text!r}")
    if value <= 0:
        raise ValueError("высота шрифта должна быть больше нуля")
    return value


def method_from_opt(value: str | None) -> str:
    """GUI value -> image_to_label method; legacy on/off kept."""
    return {"on": "floyd", "off": "threshold"}.get(value or "auto", value or "auto")


def _text_label(data: bytes, dpi: int, size: tuple[int, int], o: dict) -> tuple:
    """Uploaded text -> (1-bit image, description for the info line)."""
    text = data.decode("utf-8")
    if not text.strip():
        raise ValueError("введите текст")
    align = o.get("align") if o.get("align") in pl.TEXT_ALIGNS else "center"
    bw, font_dots, fits = pl.text_to_label(
        text, dpi, *size, parse_font_mm(str(o.get("fsize") or "")), align, bool(o.get("bold"))
    )
    info = f"текст, шрифт {font_dots * 25.4 / dpi:.1f} мм"
    return bw, info + ("" if fits else ", ⚠ не помещается — уменьшите шрифт")


def _image_label(data: bytes, dpi: int, size: tuple[int, int], o: dict) -> tuple:
    """Uploaded image -> (1-bit image, description for the info line)."""
    method = method_from_opt(o.get("dither"))
    # Pillow reads file-like objects; a temp file can't be reopened on Windows
    bw, dithered = pl.image_to_label(io.BytesIO(data), dpi, None, *size, method=method)
    if method != "auto":
        return bw, method
    return bw, f"{'floyd' if dithered else 'threshold'} (авто)"


def build_job(data: bytes, filename: str, o: dict) -> dict:
    """Build the label for the uploaded image or text; returns bytes + preview + log."""
    size = pl.parse_size(o.get("size") or "40")
    copies = max(1, int(o.get("copies") or 1))
    emulator = bool(o.get("emu"))
    out = io.StringIO()
    target = make_target(o)
    is_text = o.get("mode") == "text"
    with redirect_stdout(out):
        if not is_text and filename.lower().endswith(".prn"):
            return {"raw": data, "png": None, "info": f"готовый .prn, {len(data)} байт", "log": ""}
        dpi_opt = o.get("dpi", "auto")
        dpi = pl.detect_dpi(target) if dpi_opt == "auto" else int(dpi_opt)
        bw, mode_info = (_text_label if is_text else _image_label)(data, dpi, size, o)
        raw = pl.build_prn(bw, copies, *size, emulator)
        pl.validate_prn(raw, dpi, copies, *size, emulator)
    png = io.BytesIO()
    bw.save(png, format="PNG")
    info = f"{size[0]}x{size[1]} мм, {dpi} dpi, {bw.width}x{bw.height} точек, {mode_info}, {len(raw)} байт"
    return {"raw": raw, "png": png.getvalue(), "info": info, "log": out.getvalue()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # route http.server's access log to logging
        log.debug(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain(self, length: int) -> None:
        """Read and discard an oversized body (up to DRAIN_LIMIT) so the browser sees the 413, not a reset."""
        left = min(length, DRAIN_LIMIT)
        while left > 0:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk:
                break
            left -= len(chunk)
        if length > DRAIN_LIMIT:
            self.close_connection = True

    def do_GET(self) -> None:
        if self.path != "/":
            self._send(404, b"not found")
            return
        page = PAGE.replace("__IP__", pl.PRINTER_IP).replace("__PORT__", str(pl.PRINTER_PORT))
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        from urllib.parse import unquote

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._drain(length)
                self._send(413, f"файл больше {MAX_BODY // 2**20} МБ".encode())
                return
            body = self.rfile.read(length)
            filename = unquote(self.headers.get("X-Filename", ""))
            o = parse_opts(self.headers.get("X-Opts", "{}"))
            log.info("%s %s mode=%s file=%r bytes=%d", self.command, self.path, o.get("mode", "image"), filename, len(body))
            if self.path == "/check":
                self._check(o)
            elif not body:
                hint = "введите текст" if o.get("mode") == "text" else "выберите картинку"
                self._send(400, hint.encode())
            elif self.path == "/preview":
                self._preview(body, filename, o)
            elif self.path == "/print":
                self._print(body, filename, o)
            else:
                self._send(404, b"not found")
        except SystemExit as exc:  # print_label reports errors via sys.exit(msg)
            log.warning("%s: %s", self.path, exc.code)
            self._send(400, str(exc.code).encode())
        except Exception as exc:  # noqa: BLE001 - show any failure in the UI
            log.exception("%s failed", self.path)
            self._send(400, f"{type(exc).__name__}: {exc}".encode())
        except BaseException as exc:  # noqa: BLE001 - never kill the handler thread
            log.exception("%s crashed", self.path)
            self._send(500, f"{type(exc).__name__}: {exc}".encode())

    def _check(self, o: dict) -> None:
        target = make_target(o)
        model = pl.query_model(target)
        if target.is_windows:
            self._send(200, "Windows-очередь: запрос модели не поддерживается, задайте DPI вручную".encode())
        elif model:
            self._send(200, f"Принтер ответил: {model}".encode())
        else:
            self._send(400, f"Нет ответа от {target.describe()} (эмулятор на ~!T не отвечает — это нормально)".encode())

    def _preview(self, body: bytes, filename: str, o: dict) -> None:
        import base64

        job = build_job(body, filename, o)
        png = base64.b64encode(job["png"]).decode() if job["png"] else ""
        payload = {"png": png, "info": job["info"]}
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _print(self, body: bytes, filename: str, o: dict) -> None:
        job = build_job(body, filename, o)
        out = io.StringIO()
        with redirect_stdout(out):
            pl.send_raw(job["raw"], make_target(o))
        log.info("printed: %s", job["info"])
        self._send(200, out.getvalue().strip().encode())


class _Server(ThreadingHTTPServer):
    # SO_REUSEADDR on POSIX only: on Windows it would let a second process share a busy port
    allow_reuse_address = os.name != "nt"


def bind_server(port: int) -> ThreadingHTTPServer:
    """Try port..port+20, then an OS-assigned port."""
    for p in [*range(port, min(port + 21, 65536)), 0]:
        try:
            return _Server((HOST, p), Handler)
        except (OSError, OverflowError):
            continue
    raise OSError("не удалось занять ни один порт")


def open_browser(url: str) -> bool:
    try:
        if webbrowser.open(url):
            return True
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("win"):
        try:
            os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606
            return True
        except OSError:
            pass
    for cmd in (["xdg-open", url], ["gio", "open", url]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                pass
    return False


def _open_or_hint(url: str) -> None:
    if not open_browser(url):
        print(f"Откройте в браузере: {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_GUI_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--debug", action="store_true", help="verbose log (every request, http access log)")
    parser.add_argument("--log-file", metavar="PATH", help="also write the log to this file")
    args = parser.parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    server = bind_server(args.port)
    url = f"http://{HOST}:{server.server_address[1]}"
    print(f"GUI: {url}  (Ctrl+C — выход)")
    log.info("server started on %s", url)
    if not args.no_browser:
        threading.Timer(0.5, _open_or_hint, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
