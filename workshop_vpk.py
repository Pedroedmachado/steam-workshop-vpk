"""Steam Workshop -> VPK downloader.

Cole um ou mais links da Steam Workshop (ou IDs) e o programa baixa o
conteudo e salva como .vpk na pasta escolhida.

Fluxo por item:
  1. Consulta a Steam Web API (GetPublishedFileDetails) - nao precisa de chave.
  2. Se o item tiver file_url (itens "legados"), baixa direto.
  3. Senao, usa o SteamCMD (baixado automaticamente) com login anonimo.
  4. Copia os .vpk encontrados; arquivos .bin que sao VPK viram .vpk;
     se o item vier como pasta solta, empacota a pasta num .vpk.

Uso:
  workshop_vpk.exe                      -> abre a interface
  workshop_vpk.exe <link|id> [...] [-o pasta]  -> modo linha de comando
"""

import io
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import requests
import vpk

API_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
API_COLLECTION = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
STEAMCMD_ZIP = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
VPK_MAGIC = b"\x34\x12\xaa\x55"

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SteamWorkshopVPK"
STEAMCMD_DIR = APP_DIR / "steamcmd"
STEAMCMD_EXE = STEAMCMD_DIR / "steamcmd.exe"
DEFAULT_OUT = Path.home() / "Downloads" / "Workshop VPK"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WorkshopError(Exception):
    pass


def parse_ids(text):
    """Extrai IDs de links (?id=123) ou numeros soltos."""
    ids = []
    for token in re.split(r"\s+", text.strip()):
        if not token:
            continue
        m = re.search(r"[?&]id=(\d+)", token) or re.fullmatch(r"(\d{5,})", token)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def get_details(ids):
    data = {"itemcount": len(ids)}
    for i, pid in enumerate(ids):
        data[f"publishedfileids[{i}]"] = pid
    r = requests.post(API_DETAILS, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["response"].get("publishedfiledetails", [])


def expand_collection(pid):
    r = requests.post(API_COLLECTION, data={"collectioncount": 1, "publishedfileids[0]": pid}, timeout=30)
    r.raise_for_status()
    details = r.json()["response"].get("collectiondetails", [])
    if not details or details[0].get("result") != 1:
        return []
    return [c["publishedfileid"] for c in details[0].get("children", [])]


def safe_name(text, limit=80):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip(" .")
    return text[:limit] or "item"


def is_vpk_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == VPK_MAGIC
    except OSError:
        return False


def maybe_decompress_lzma(path):
    """Itens antigos do Garry's Mod vem como .gma comprimido em LZMA."""
    import lzma
    with open(path, "rb") as f:
        head = f.read(4)
    if head == b"GMAD" or head == VPK_MAGIC:
        return
    try:
        with open(path, "rb") as f:
            data = lzma.decompress(f.read(), format=lzma.FORMAT_ALONE)
    except lzma.LZMAError:
        return
    with open(path, "wb") as f:
        f.write(data)


def extract_gma(path, dest):
    """Extrai um addon .gma (Garry's Mod) para uma pasta."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"GMAD":
        return False
    pos = 4
    version = data[pos]
    pos += 1 + 8 + 8  # versao, steamid, timestamp

    def cstr():
        nonlocal pos
        end = data.index(b"\x00", pos)
        s = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    if version > 1:
        while cstr():
            pass
    cstr(); cstr(); cstr()  # nome, descricao, autor
    pos += 4  # versao do addon
    entries = []
    while True:
        (num,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if num == 0:
            break
        name = cstr()
        size, _crc = struct.unpack_from("<qI", data, pos)
        pos += 12
        entries.append((name, size))
    for name, size in entries:
        out = Path(dest) / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data[pos:pos + size])
        pos += size
    return True


def unique_path(path):
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


class Downloader:
    def __init__(self, out_dir, log=print, progress=None):
        self.out_dir = Path(out_dir)
        self.log = log
        self.progress = progress or (lambda frac: None)

    # ---------- SteamCMD ----------
    def ensure_steamcmd(self):
        if STEAMCMD_EXE.exists():
            return
        self.log("Baixando SteamCMD (so na primeira vez)...")
        STEAMCMD_DIR.mkdir(parents=True, exist_ok=True)
        r = requests.get(STEAMCMD_ZIP, timeout=60)
        r.raise_for_status()
        zipfile.ZipFile(io.BytesIO(r.content)).extractall(STEAMCMD_DIR)
        self.log("SteamCMD instalado. A primeira execucao se atualiza e pode demorar um pouco.")

    def steamcmd_download(self, appid, pid):
        self.ensure_steamcmd()
        cmd = [str(STEAMCMD_EXE), "+login", "anonymous",
               "+workshop_download_item", str(appid), str(pid), "validate", "+quit"]
        self.log(f"SteamCMD: baixando item {pid} (app {appid})...")
        proc = subprocess.Popen(cmd, cwd=STEAMCMD_DIR, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=NO_WINDOW, text=True, errors="replace")
        lines = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            lines.append(line)
            if any(k in line for k in ("Success", "ERROR", "Downloading item", "Update state", "FAILED")):
                self.log("  " + line)
        proc.wait()
        content = STEAMCMD_DIR / "steamapps" / "workshop" / "content" / str(appid) / str(pid)
        if not content.exists() or not any(content.rglob("*")):
            last = next((l for l in reversed(lines) if "ERROR" in l or "FAILED" in l), "")
            hint = ""
            if "Access Denied" in last or "No subscription" in last or "Failure" in last:
                hint = (" (esse jogo nao permite download anonimo da Workshop - "
                        "e preciso ter o jogo na conta Steam)")
            raise WorkshopError(f"SteamCMD nao baixou o item. {last}{hint}")
        return content

    # ---------- Download direto ----------
    def http_download(self, url, dest):
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.progress(done / total)
        self.progress(1.0)

    # ---------- Conversao para VPK ----------
    def export_folder(self, folder, base_name):
        """Copia VPKs da pasta baixada; empacota a pasta se nao houver nenhum."""
        saved = []
        files = [p for p in folder.rglob("*") if p.is_file()]
        vpks = [p for p in files if p.suffix.lower() == ".vpk" or is_vpk_file(p)]
        # Ignora pedacos _000.vpk de VPKs multi-arquivo; copia o _dir junto com eles
        for src in vpks:
            if re.search(r"_\d{3}\.vpk$", src.name, re.I):
                dest = self.out_dir / src.name
                shutil.copy2(src, dest)
                continue
            name = base_name if len(vpks) == 1 else f"{base_name}_{safe_name(src.stem)}"
            dest = unique_path(self.out_dir / f"{name}.vpk")
            shutil.copy2(src, dest)
            saved.append(dest)
        if saved:
            return saved
        if not files:
            raise WorkshopError("O item veio vazio.")
        # Garry's Mod: .gma (as vezes comprimido) -> extrai antes de empacotar
        unpacked = APP_DIR / "tmp" / f"gma_{base_name[:20]}"
        shutil.rmtree(unpacked, ignore_errors=True)
        got_gma = False
        for p in files:
            maybe_decompress_lzma(p)
            if extract_gma(p, unpacked):
                self.log(f"  Addon .gma detectado ({p.name}) - extraindo...")
                got_gma = True
        if got_gma:
            folder = unpacked
        self.log("  Nenhum .vpk no item - empacotando a pasta em .vpk...")
        dest = unique_path(self.out_dir / f"{base_name}.vpk")
        vpk.new(str(folder)).save(str(dest))
        shutil.rmtree(unpacked, ignore_errors=True)
        return [dest]

    # ---------- Fluxo principal ----------
    def run(self, ids):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        queue = list(ids)
        results, errors = [], []
        details = {}
        # Resolve colecoes em itens
        for d in get_details(queue):
            if d.get("result") != 1:
                errors.append((d.get("publishedfileid"), "item nao encontrado, privado ou removido"))
                continue
            children = expand_collection(d["publishedfileid"]) if not d.get("consumer_app_id") or d.get("file_size") in (None, "0", 0) else []
            if children:
                self.log(f"Colecao '{d.get('title')}' com {len(children)} itens.")
                for c in get_details(children):
                    if c.get("result") == 1:
                        details[c["publishedfileid"]] = c
                    else:
                        errors.append((c.get("publishedfileid"), "item da colecao indisponivel"))
            else:
                details[d["publishedfileid"]] = d

        for n, (pid, d) in enumerate(details.items(), 1):
            title = d.get("title") or pid
            appid = d.get("consumer_app_id")
            base = f"{pid}_{safe_name(title)}"
            self.log(f"\n[{n}/{len(details)}] {title}  (id {pid}, app {appid})")
            self.progress(0)
            try:
                url = d.get("file_url")
                tmp = self.out_dir / f"{base}.part"
                if url:
                    self.log("  Download direto pela Steam...")
                    try:
                        self.http_download(url, tmp)
                    except Exception as e:
                        self.log(f"  Download direto falhou ({e}); tentando pelo SteamCMD...")
                        tmp.unlink(missing_ok=True)
                        url = None
                if url:
                    if is_vpk_file(tmp) or str(d.get("filename", "")).lower().endswith(".vpk"):
                        dest = unique_path(self.out_dir / f"{base}.vpk")
                        tmp.replace(dest)
                        saved = [dest]
                    else:
                        # Pode ser zip/outro formato: extrai e empacota
                        work = APP_DIR / "tmp" / pid
                        shutil.rmtree(work, ignore_errors=True)
                        work.mkdir(parents=True)
                        if zipfile.is_zipfile(tmp):
                            zipfile.ZipFile(tmp).extractall(work)
                        else:
                            shutil.move(str(tmp), work / (Path(d.get("filename") or "file").name))
                        tmp.unlink(missing_ok=True)
                        saved = self.export_folder(work, base)
                        shutil.rmtree(work, ignore_errors=True)
                else:
                    folder = self.steamcmd_download(appid, pid)
                    saved = self.export_folder(folder, base)
                    shutil.rmtree(folder, ignore_errors=True)
                for s in saved:
                    self.log(f"  OK -> {s}")
                results.extend(saved)
            except Exception as e:
                self.log(f"  ERRO: {e}")
                errors.append((pid, str(e)))
            self.progress(1.0)

        self.log(f"\nConcluido: {len(results)} arquivo(s) salvo(s) em {self.out_dir}")
        for pid, msg in errors:
            self.log(f"  Falhou {pid}: {msg}")
        return results, errors


# ---------------- Interface ----------------
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Steam Workshop -> VPK")
    root.geometry("720x520")
    root.minsize(520, 400)

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Links da Steam Workshop (um por linha, aceita colecoes):").pack(anchor="w")
    links = tk.Text(frm, height=5, wrap="none")
    links.pack(fill="x", pady=(2, 8))

    row = ttk.Frame(frm)
    row.pack(fill="x")
    ttk.Label(row, text="Salvar em:").pack(side="left")
    out_var = tk.StringVar(value=str(DEFAULT_OUT))
    ttk.Entry(row, textvariable=out_var).pack(side="left", fill="x", expand=True, padx=6)

    def pick():
        d = filedialog.askdirectory(initialdir=out_var.get())
        if d:
            out_var.set(d)

    ttk.Button(row, text="Procurar...", command=pick).pack(side="left")

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=8)
    go = ttk.Button(btns, text="Baixar .vpk")
    go.pack(side="left")
    open_btn = ttk.Button(btns, text="Abrir pasta",
                          command=lambda: os.startfile(out_var.get()) if Path(out_var.get()).exists() else None)
    open_btn.pack(side="left", padx=6)

    bar = ttk.Progressbar(frm, maximum=1.0)
    bar.pack(fill="x")

    log_box = tk.Text(frm, height=14, state="disabled", wrap="word", font=("Consolas", 9))
    log_box.pack(fill="both", expand=True, pady=(8, 0))

    def log(msg):
        def _do():
            log_box.configure(state="normal")
            log_box.insert("end", msg + "\n")
            log_box.see("end")
            log_box.configure(state="disabled")
        root.after(0, _do)

    def progress(frac):
        root.after(0, lambda: bar.configure(value=frac))

    def start():
        ids = parse_ids(links.get("1.0", "end"))
        if not ids:
            messagebox.showwarning("Aviso", "Cole pelo menos um link valido da Steam Workshop.")
            return
        go.configure(state="disabled")

        def work():
            try:
                Downloader(out_var.get(), log, progress).run(ids)
            except Exception as e:
                log(f"ERRO: {e}")
            finally:
                root.after(0, lambda: go.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    go.configure(command=start)
    root.mainloop()


def main():
    args = sys.argv[1:]
    if not args:
        run_gui()
        return
    out = DEFAULT_OUT
    if "-o" in args:
        i = args.index("-o")
        out = Path(args[i + 1])
        del args[i:i + 2]
    ids = parse_ids(" ".join(args))
    if not ids:
        print(__doc__)
        sys.exit(1)
    _, errors = Downloader(out).run(ids)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
