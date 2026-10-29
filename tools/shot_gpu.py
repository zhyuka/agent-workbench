"""GPU-on full-page screenshot via CDP. A screenshot taken with --disable-gpu drops the paint of
text/content over translucent + backdrop-filter surfaces (a known false negative). This uses
SwiftShader so those surfaces composite correctly. Before capturing it waits for every <img> to load
and decode, prints a `# settle:` line, and names each image that never loaded or did not decode.
Edge picks its own free DevTools port, and the script refuses to capture any page but the one it
launched.

Usage: python tools/shot_gpu.py <url> <width> <out.png> [hoverSelector] [clipSelector]
A hover or clip selector that matches nothing exits 2 and captures nothing.
The browser is $EDGE_PATH or $CHROME_PATH, else the usual Edge and Chrome install locations on
Windows and macOS, else msedge, google-chrome, chromium or chrome on PATH."""
import json,os,subprocess,sys,tempfile,time,base64,socket,urllib.request,struct

# Answer -h/--help with this module's own docstring, before any module-level work can pre-empt it.
if __name__ == "__main__" and any(_a in ("-h", "--help") for _a in __import__("sys").argv[1:]):
    print((__doc__ or "").strip() or "No docstring.")
    raise SystemExit(0)

_WIN = os.name == "nt"
# ★ SCROLL ONLY WHAT A VISITOR CAN. scrollIntoView also scrolls an overflow:hidden
# ancestor whenever the target is cut by it and could fit inside it (a 20px element 8px past a 30px
# box's edge scrolls the box by 8), so the hover lands on a state the page never shows.
# This centres the target in each overflow:auto or scroll box, which a visitor can scroll, then in
# the window, and leaves a hidden or clip box where the page has it.
TO_VIEW_JS = ("e=>{for(let a=e.parentElement;a&&a!==document.body&&a!==document.documentElement;a=a.parentElement){"
              "const s=getComputedStyle(a);const y=/auto|scroll/.test(s.overflowY)&&a.scrollHeight>a.clientHeight;"
              "const x=/auto|scroll/.test(s.overflowX)&&a.scrollWidth>a.clientWidth;if(!x&&!y)continue;"
              "const r=e.getBoundingClientRect(),q=a.getBoundingClientRect();"
              "if(y)a.scrollTop+=r.top-q.top-a.clientTop-Math.max(0,(a.clientHeight-r.height)/2);"
              "if(x)a.scrollLeft+=r.left-q.left-a.clientLeft-Math.max(0,(a.clientWidth-r.width)/2);}"
              "const r=e.getBoundingClientRect();const dy=r.height>innerHeight?r.top:r.top-(innerHeight-r.height)/2;"
              "window.scrollTo({left:scrollX,top:scrollY+dy,behavior:'instant'});}")
def _find_browser():
    """Locate a Chromium browser. Order: $EDGE_PATH / $CHROME_PATH, then the usual
    Edge and Chrome install locations on Windows and macOS, then PATH."""
    import shutil
    c = os.environ.get("EDGE_PATH") or os.environ.get("CHROME_PATH")
    if c and os.path.exists(c):
        return c
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if p and os.path.exists(p):
            return p
    for n in ("msedge", "google-chrome", "chromium", "chrome"):
        p = shutil.which(n)
        if p:
            return p
    sys.exit("No Chromium browser found. Install Microsoft Edge, or set EDGE_PATH "
             "to the full path of msedge.exe / chrome.")


if len(sys.argv)<4:
    print(next((l for l in (__doc__ or "").splitlines() if l.startswith("Usage:")),"Usage: see --help"),
          file=sys.stderr)
    sys.exit(2)
EDGE = _find_browser()
url,width,out=sys.argv[1],int(sys.argv[2]),sys.argv[3]
HOVER=sys.argv[4] if len(sys.argv)>4 else None
CLIP=sys.argv[5] if len(sys.argv)>5 else None
# Let Edge pick its own free DevTools port (--remote-debugging-port=0) and read back the one it bound
# from <prof>/DevToolsActivePort. A fixed port is not safe: if another browser already holds it on
# 127.0.0.1, our Edge binds [::1] instead, and polling 127.0.0.1 attaches to the OTHER browser and
# captures its page (an orphaned browser left by another tool is enough).
# mkdtemp gives a fresh, empty profile, so a DevToolsActivePort in it can only be our Edge's; the
# random suffix also stops _reap's `pkill -f gpushot-123` from matching another run's gpushot-1234.
prof=tempfile.mkdtemp(prefix=f"gpushot-{os.getpid()}-")
# doNotTrack on in the fresh profile, so an analytics script that honours it never counts this run.
# A pref, not an injected script: the URL loads at launch, before anything can attach.
os.makedirs(os.path.join(prof,"Default"))
with open(os.path.join(prof,"Default","Preferences"),"w",encoding="utf-8") as f:
  json.dump({"enable_do_not_track":True},f)
# Edge writes its temp files to the user temp dir, not the profile, and leaves them when it exits;
# a managed install can unpack policy extensions there for every fresh profile, hundreds of MB a run,
# and _reap deletes the profile and nothing else. In the profile, _reap deletes them with it.
# Chromium on macOS reads MAC_CHROMIUM_TMPDIR first (with it set, nothing reaches the user temp
# dir). TEMP and TMP are for Windows, not measured there.
tmp=os.path.join(prof,"tmp")
os.makedirs(tmp)
env=dict(os.environ,MAC_CHROMIUM_TMPDIR=tmp,TMPDIR=tmp,TEMP=tmp,TMP=tmp)
p=subprocess.Popen([EDGE,"--headless=new","--use-angle=swiftshader","--enable-unsafe-swiftshader",
  "--no-sandbox","--remote-debugging-port=0",f"--user-data-dir={prof}","--hide-scrollbars",url],
  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
def _page_key(u):
    # What must match between the URL we launched and the page we attach to: scheme, host, port and
    # path. Query, fragment and a trailing slash are ignored, so a /x -> /x/ redirect still matches.
    from urllib.parse import urlsplit,unquote
    s=urlsplit(u)
    try: pt=s.port
    except ValueError: pt=None
    return (s.scheme,s.hostname,pt or {"http":80,"https":443}.get(s.scheme),unquote(s.path).rstrip("/"))
# Edge turns a file path argument into a file:// URL; compare against that form.
want=__import__("pathlib").Path(url).resolve().as_uri() if os.path.exists(url) else url
def kill():
    if _WIN:
        subprocess.run(["taskkill","/F","/T","/PID",str(p.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:
        try: p.terminate()
        except Exception: pass
    _reap(prof)
def _reap(prof):
    # Delete our own --user-data-dir. taskkill /T is NOT enough: Edge re-parents its renderer, so it
    # survives the tree-kill, keeps a handle on the profile, and the rmtree fails silently. Match on
    # the profile path and kill that first.
    import shutil, time
    leaf = os.path.basename(prof)
    try:
        if _WIN:
            ps = ("Get-CimInstance Win32_Process -Filter \"name='msedge.exe' OR name='chrome.exe'\" | "
                  "Where-Object { $_.CommandLine -match [regex]::Escape('" + leaf + "') } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
            subprocess.run(["powershell","-NoProfile","-Command",ps],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=25)
        else:
            subprocess.run(["pkill","-9","-f",leaf],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=25)
    except Exception: pass
    for _ in range(12):
        shutil.rmtree(prof, ignore_errors=True)
        if not os.path.isdir(prof): return
        time.sleep(0.5)
# A run killed outright (SIGKILL, or a caller's timeout) never reaches kill(), so its profile stays
# with Edge's temp files inside it, often hundreds of MB. pw_temp.reap deletes those at each launch,
# with every other tool's pid-named folders: the pid in the name and an age floor tell a dead run's
# folder from a live one. pw_temp.PREFIXES lists gpushot-.
import pw_temp
pw_temp.reap()
try:
    ws=None; port=None; seen=[]
    for _ in range(100):
        if port is None:
            try:
                with open(os.path.join(prof,"DevToolsActivePort")) as f: line=f.readline()
                if line.endswith("\n"): port=int(line)   # no newline yet = Edge is still writing it
            except (OSError,ValueError): pass
            if port is None and p.poll() is not None: break
        if port is not None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as r:
                    pages=[t for t in json.load(r) if t.get("type")=="page" and "devtools" not in t.get("url","")]
                seen=[t.get("url","") for t in pages]
                for t in pages:
                    if _page_key(t.get("url",""))==_page_key(want): ws=t["webSocketDebuggerUrl"];break
                if ws:break
            except Exception:pass
        time.sleep(.25)
    if not ws:
        if port is None:
            sys.exit(f"shot_gpu: Edge never reported a DevTools port (no DevToolsActivePort in {prof}, "
                     f"Edge exit code {p.poll()}). Nothing captured.")
        if seen:
            sys.exit(f"shot_gpu: refusing to capture. The page on our Edge's port {port} is {seen}, "
                     f"not {url}. A screenshot of the wrong page is worse than none.")
        sys.exit(f"shot_gpu: no page target on our Edge's port {port}. Nothing captured.")
    host,rest=ws.split("://")[1].split("/",1)
    h,pt=host.split(":");s=socket.create_connection((h,int(pt)));s.settimeout(60)
    key=base64.b64encode(os.urandom(16)).decode()
    s.sendall(f"GET /{rest} HTTP/1.1\r\nHost:{host}\r\nUpgrade:websocket\r\nConnection:Upgrade\r\nSec-WebSocket-Key:{key}\r\nSec-WebSocket-Version:13\r\n\r\n".encode())
    buf=b""
    while b"\r\n\r\n" not in buf: buf+=s.recv(4096)
    def send(i,m,pr=None):
        msg=json.dumps({"id":i,"method":m,"params":pr or {}}).encode()
        hdr=bytearray([0x81]);n=len(msg)
        if n<126:hdr.append(0x80|n)
        elif n<65536:hdr.append(0x80|126);hdr+=struct.pack(">H",n)
        else:hdr.append(0x80|127);hdr+=struct.pack(">Q",n)
        mk=os.urandom(4);hdr+=mk
        s.sendall(bytes(hdr)+bytes(b^mk[j%4] for j,b in enumerate(msg)))
    def recv():
        while True:
            hd=s.recv(2)
            if len(hd)<2: return None
            ln=hd[1]&127
            if ln==126: ln=struct.unpack(">H",s.recv(2))[0]
            elif ln==127: ln=struct.unpack(">Q",s.recv(8))[0]
            d=b""
            while len(d)<ln: d+=s.recv(min(65536,ln-len(d)))
            try: return json.loads(d)
            except Exception: return {}
    i=[0]
    def cmd(m,pr=None):
        i[0]+=1;send(i[0],m,pr)
        while True:
            r=recv()
            if r is None: return {}
            if r.get("id")==i[0]: return r.get("result",{})
    cmd("Page.enable");cmd("Runtime.enable")
    # Apply emulation and PROVE it took, retrying, because the override races the page load and a
    # single un-checked call silently does nothing (asking for 390 without the check can give a
    # 980-wide capture).
    got=None
    for _ in range(15):
        cmd("Emulation.setDeviceMetricsOverride",{"width":width,"height":900,"deviceScaleFactor":1,"mobile":width<768})
        time.sleep(0.35)
        try: got=int(cmd("Runtime.evaluate",{"expression":"document.documentElement.clientWidth","returnByValue":True}).get("result",{}).get("value",0))
        except Exception: got=None
        if got==width: break
    if got!=width:
        raise SystemExit(f"shot_gpu: emulation did not take (asked {width}, layout reports {got}). "
                         f"Refusing to capture -- a screenshot at the wrong viewport is worse than none.")
    time.sleep(1.5)
    # reveal every scroll-animated block, then settle
    cmd("Runtime.evaluate",{"expression":"document.querySelectorAll('[data-animate]').forEach(e=>e.classList.add('is-visible'));'ok'"})
    time.sleep(1.2)
    # DO NOT resize the viewport to scrollHeight. `captureBeyondViewport` below already grabs the
    # whole page, and a resize lies: any element sized off the viewport (100svh, or a hero that fills
    # the screen) grows to match the new height, which grows scrollHeight, which grows the element
    # again (a 390x5113 request can produce a 390x9326 PNG). The viewport stays at the height
    # set above, so the layout is the one the page actually has.
    #
    # The two things a resize would do by accident are done on purpose:
    #   1. pulling lazy <img> below the fold into view -> scroll through the page, then return to top
    #   2. putting a far-down hover target inside the viewport -> scrollIntoView before dispatching
    # Every wait here is BOUNDED. An unbounded await on image decode or document.fonts.ready hangs
    # the CDP socket instead of failing, which reads as a dead tool rather than a slow page.
    cmd("Runtime.evaluate",{"expression":"""(async()=>{
        const cap=ms=>new Promise(r=>setTimeout(r,ms));
        const step=Math.max(400,innerHeight-100);
        const max=document.documentElement.scrollHeight;
        for(let y=0,n=0;y<max&&n<40;y+=step,n++){window.scrollTo(0,y);await cap(80);}
        window.scrollTo(0,0);await cap(150);
        return 'ok';})()""","awaitPromise":True,"returnByValue":True})
    def ev(expr,wait=False):
        return cmd("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":wait}).get("result",{}).get("value")
    def settle_images(load_s=10.0,decode_ms=5000,quiet_ms=500,net_s=5.0):
        # `complete` is not enough: a decoding="async" hero can read complete=true, naturalWidth=2000
        # and still draw as flat body background. So: poll until each <img> is complete with
        # naturalWidth>0, decode every loaded one, wait for the network to go quiet, fonts.ready and
        # two frames. Name every image that never got there instead of hiding it.
        t0=time.time(); unloaded=None
        while True:
            u=ev("""JSON.stringify([...document.images].filter(i=>!(i.complete&&i.naturalWidth>0))
              .map(i=>(i.currentSrc||i.getAttribute('src')||'(no src)')+' complete='+i.complete+' naturalWidth='+i.naturalWidth))""")
            if u is not None: unloaded=json.loads(u)
            if unloaded==[] or time.time()-t0>load_s: break
            time.sleep(0.1)
        n=ev("document.images.length") or 0
        t_load=time.time()-t0
        undecoded=ev("""(async()=>{const cap=ms=>new Promise(r=>setTimeout(()=>r('no answer in '+ms+'ms'),ms));
          const im=[...document.images].filter(i=>i.complete&&i.naturalWidth>0);
          const src=im.map(i=>i.currentSrc||i.src);  // read BEFORE decode: a src swap rejects it, then names the new URL
          const r=await Promise.all(im.map(i=>Promise.race([i.decode().then(()=>'ok',e=>e.name||'error'),cap(%d)])));
          return JSON.stringify(src.map((u,k)=>[u,r[k]]).filter(x=>x[1]!=='ok'));})()"""%decode_ms,True)
        undecoded=None if undecoded is None else json.loads(undecoded)
        t_dec=time.time()-t0
        # Resource-timing entries land when a response ENDS, so "quiet" means nothing new finished
        # for quiet_ms, not that nothing is in flight. The default buffer stops at 250 entries, which
        # would read as quiet forever; raise it first.
        ev("performance.setResourceTimingBufferSize(100000);1")
        t1=time.time(); last=None; since=t1; count=0
        while time.time()-t1<net_s:
            count=ev("performance.getEntriesByType('resource').length") or 0
            if count!=last: last,since=count,time.time()
            elif (time.time()-since)*1000>=quiet_ms: break
            time.sleep(0.1)
        quiet=(time.time()-since)*1000>=quiet_ms
        fonts=ev("""(async()=>{const cap=ms=>new Promise(r=>setTimeout(()=>r('fonts not ready in '+ms+'ms'),ms));
          const f=document.fonts&&document.fonts.ready?Promise.race([document.fonts.ready.then(()=>'fonts ready'),cap(3000)]):'no FontFaceSet';
          const out=await f; await Promise.race([new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))),cap(1000)]);
          return out;})()""",True)
        loaded=None if unloaded is None else n-len(unloaded)
        print(f"# settle: "
              +(f"{loaded} of {n} <img> loaded ({t_load:.1f}s), " if unloaded is not None else "load check DID NOT RUN, ")
              +(f"{(n if loaded is None else loaded)-len(undecoded)} decoded ({t_dec:.1f}s)" if undecoded is not None else "decode check DID NOT RUN")
              +f"; network {'quiet' if quiet else 'STILL BUSY'}: {count} resources, "
              +(f"none new for {quiet_ms}ms" if quiet else f"still changing after {net_s:.0f}s")
              +f"; {fonts or 'fonts check DID NOT RUN'}")
        for u in unloaded or []:
            print(f"# IMAGE NEVER LOADED in {load_s:.0f}s, captured without it: {u}")
        for u,why in undecoded or []:
            print(f"# IMAGE DID NOT DECODE ({why}), may be missing from the capture: {u}")
    settle_images()
    time.sleep(1.0)
    if HOVER:
        # scroll the target into view FIRST: dispatchMouseEvent takes viewport coordinates, so a
        # target below the fold would otherwise be hovered at a point that is not on it.
        box=cmd("Runtime.evaluate",{"expression":"(()=>{const e=document.querySelector(%r);if(!e)return null;(%s)(e);const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()"%(HOVER,TO_VIEW_JS),"returnByValue":True}).get("result",{}).get("value")
        if not box:
            # A shot taken at rest would pass for the hover state; refuse instead.
            print(f"shot_gpu: DID NOT RUN: hover selector {HOVER!r} matched nothing. Nothing captured.",
                  file=sys.stderr)
            sys.exit(2)
        cmd("Input.dispatchMouseEvent",{"type":"mouseMoved","x":box["x"],"y":box["y"]})
        time.sleep(1.2)
    clip=None
    if CLIP:
        # clip is DOCUMENT-relative when capturing beyond the viewport, but getBoundingClientRect is
        # viewport-relative, so add the scroll offset. Without this the clip lands wherever the page
        # happens to be scrolled to.
        cb=cmd("Runtime.evaluate",{"expression":"(()=>{const e=document.querySelector(%r);if(!e)return null;const r=e.getBoundingClientRect();return {x:Math.round(r.x+window.scrollX),y:Math.round(r.y+window.scrollY),width:Math.round(r.width),height:Math.round(r.height),scale:1};})()"%CLIP,"returnByValue":True}).get("result",{}).get("value")
        if not cb:
            # A full-page shot would pass for the clipped one; refuse instead.
            print(f"shot_gpu: DID NOT RUN: clip selector {CLIP!r} matched nothing. Nothing captured.",
                  file=sys.stderr)
            sys.exit(2)
        clip=cb
    if not clip:
        # Pass an EXPLICIT full-document clip rather than trusting captureBeyondViewport on its own.
        # Chromium's handling of that flag varies with the emulation mode: the same page can capture
        # full-height at 390 (mobile:true) but only 1440x900, the bare viewport, at 1440. An explicit
        # clip is deterministic at every width.
        doc=cmd("Runtime.evaluate",{"expression":"(()=>{const d=document.documentElement,b=document.body;return {w:Math.max(d.scrollWidth,b?b.scrollWidth:0),h:Math.max(d.scrollHeight,b?b.scrollHeight:0)};})()","returnByValue":True}).get("result",{}).get("value") or {}
        if doc.get("h"): clip={"x":0,"y":0,"width":int(doc.get("w") or width),"height":int(doc["h"]),"scale":1}
    pr={"format":"png","captureBeyondViewport":True}
    if clip: pr["clip"]=clip
    r=cmd("Page.captureScreenshot",pr)
    png=base64.b64decode(r["data"])
    open(out,"wb").write(png)
    # Report the dimensions of the FILE, read back out of the PNG's IHDR, not the ones we asked for:
    # the request can say 390x2262 for a capture that is 980x5684, and a tool that states a number it
    # did not measure launders every check built on it.
    if len(png)>=24 and png[12:16]==b"IHDR":
        aw=int.from_bytes(png[16:20],"big"); ah=int.from_bytes(png[20:24],"big")
        warn="" if aw==width or CLIP else f"  <-- WARNING: asked for width {width}"
        print("saved",out,f"{aw}x{ah}{warn}")
    else:
        print("saved",out,"(could not read PNG header -- dimensions unverified)")
finally:
    kill()
