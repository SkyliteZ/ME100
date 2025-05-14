"""
Gate Control Web UI for ESP32 (MicroPython)

Features:
- Multiple feed times scheduling (persistent in config.json)
- Web interface for add/delete feed times and manual controls
- Automatic open/close on schedule or weight threshold
- NeoPixel status indicator
- Non-blocking load cell TCP listener via select
- NTP time synchronization
- Clickable Wi-Fi URL in Thonny with timeout and UI error handling
"""

# ===== Imports =====
from machine import Pin, PWM
import time, sys, select, socket, network, neopixel, ntp_server, ure, ujson

# Precompile regex
ADD_FEED_RE = ure.compile(r'hour=(\d+)&minute=(\d+)')
DEL_FEED_RE = ure.compile(r'idx=(\d+)')

# ===== Constants =====
NETWORK_CONFIG   = {'ssid': 'Berkeley-IoT', 'password': '!MOj"-17'}
CONFIG_FILE      = 'config.json'
LOADCELL_PORT    = 12345
HTTP_PORT        = 80
WEIGHT_THRESHOLD = 10      # grams
EAR_INTERVAL     = 2000    # ms
TIMEZONE_OFFSET  = -7 * 3600
WIFI_TIMEOUT     = 30      # seconds

# ===== Global State =====
feed_times       = []
last_trigger_day = {}
state            = {'opened': False}
weight           = 0.0
wifi_url         = None
wifi_error       = False

# ===== HTML Template =====
HTML_TEMPLATE = """HTTP/1.0 200 OK
Content-Type: text/html

<html>
<head><title>SNACKZILLA</title></head>
<body>
  <h1>SNACKZILLA</h1>
  <p><strong>Time:</strong> {time}</p>
  <p><strong>Weight:</strong> {weight:.2f} g</p>
  {link_html}
  <h2>Feed Times</h2><ul>{items}</ul>
  <h3>Add Feed Time</h3>
  <form action="/addFeed">
    <input name="hour" type="number" min="0" max="23"> :
    <input name="minute" type="number" min="0" max="59">
    <input type="submit" value="Add">
  </form>
  <h3>Manual Control</h3>
  <button onclick="location.href='/openNow'">Open</button>
  <button onclick="location.href='/closeNow'">Close</button>
  <button onclick="location.href='/retareNow'">Tare</button>
  <button onclick="location.href='/resetTime'">Reset</button>
</body>
</html>"""

# ===== Persistence =====
def load_config():
    global feed_times
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = ujson.load(f)
            feed_times = cfg.get('times', [])
    except (OSError, ValueError):
        feed_times = []
        save_config()


def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            ujson.dump({'times': feed_times}, f)
    except OSError as e:
        print('Error saving config:', e)

# ===== Networking =====
def connect_wifi():
    global wifi_url, wifi_error
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        wlan.disconnect(); time.sleep(1)
    wlan.connect('Berkeley-IoT', '7$mw52S')
    start = time.time()
    while not wlan.isconnected():
        if time.time() - start > WIFI_TIMEOUT:
            print('Wi-Fi connection timed out')
            wifi_error = True
            wifi_url = None
            return
        time.sleep(1)
    ip = wlan.ifconfig()[0]
    wifi_url = f"http://{ip}"
    print('Wi-Fi URL:', wifi_url)


def sync_time():
    ntp_server.host = 'pool.ntp.org'
    ntp_server.settime()
    return TIMEZONE_OFFSET

# ===== Hardware =====
def init_neopixel():
    np = neopixel.NeoPixel(Pin(14), 1)
    np.fill((0,0,0)); np.write()
    return np


def init_servos():
    gate = PWM(Pin(12), freq=50)
    sr   = PWM(Pin(27), freq=50)
    sl   = PWM(Pin(33), freq=50)
    return gate, sr, sl


def open_gate(gate):
    # Only actuate if not already open
    if not state['opened']:
        gate.duty(40)
        state['opened'] = True


def close_gate(gate):
    # Only actuate if currently open
    if state['opened']:
        gate.duty(115)
        state['opened'] = False

# ===== Load Cell TCP Server =====
def init_loadcell_server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', LOADCELL_PORT))
    s.listen(1)
    s.settimeout(0)
    print('Loadcell server listening')
    return s

# ===== HTTP Server =====
def init_http_server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', HTTP_PORT))
    s.listen(1)
    s.settimeout(0)
    print('HTTP listening on port', HTTP_PORT)
    return s


def handle_http(client, gate, sr, sl, conn, tz):
    global feed_times, last_trigger_day, weight, wifi_error, wifi_url
    try:
        req = client.recv(1024).decode()
        path = req.split()[1]
    except:
        client.close(); return

    if wifi_error:
        err = (
            'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n'
            '<html><body><h1>Wi-Fi Error</h1>'
            f'<p>Timeout {WIFI_TIMEOUT}s</p>'
            '</body></html>'
        )
        client.send(err); client.close(); return

    if path.startswith('/addFeed'):
        m = ADD_FEED_RE.search(path)
        if m:
            t = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
            if t not in feed_times:
                feed_times.append(t); save_config(); last_trigger_day.pop(t, None)
    elif path.startswith('/deleteFeed'):
        m = DEL_FEED_RE.search(path)
        if m:
            i = int(m.group(1))
            if 0 <= i < len(feed_times):
                last_trigger_day.pop(feed_times[i], None); feed_times.pop(i); save_config()
    elif path.startswith('/openNow'):
        # force-open the gate regardless of previous state,
        # update the state flag, and animate ears
        gate.duty(40)
        state['opened'] = True
        animate_ears_open(sr, sl)

    elif path.startswith('/closeNow'):
        # force-close, update flag, animate
        gate.duty(115)
        state['opened'] = False
        animate_ears_close(sr, sl)

    elif path.startswith('/retareNow'):
        try: conn.send(b'TARE\n')
        except: pass
    elif path.startswith('/resetTime'):
        feed_times.clear(); save_config(); last_trigger_day.clear()

    now = time.localtime(time.time() + tz)
    ct = f"{now[3]:02d}:{now[4]:02d}:{now[5]:02d}"
    items = ''.join(f"<li>{t} <a href=\"/deleteFeed?idx={i}\">Delete</a></li>" for i,t in enumerate(feed_times))
    link_html = f"<p><a href=\"{wifi_url}\">Dashboard</a></p>" if wifi_url else ''
    html = HTML_TEMPLATE.format(time=ct, weight=weight, link_html=link_html, items=items)
    client.send(html)
    client.close()

# ===== Animations =====
def animate_ears_open(sr, sl):
    duty90 = int(40 + (90/180)*75)
    duty0 = int(40 + (0/180)*75)
    for _ in range(2):
        sr.duty(duty90); sl.duty(duty90)
        time.sleep(0.5)
        sr.duty(duty0); sl.duty(duty0)
        time.sleep(0.5)


def animate_ears_close(sr, sl):
    duty180 = int(40 + (180/180)*75)
    sr.duty(0); sl.duty(0)

# ===== Main =====
def main():
    load_config(); connect_wifi(); tz = sync_time()
    init_neopixel(); gate, sr, sl = init_servos()
    load_srv = init_loadcell_server(); conn = None; web = init_http_server()
    last_ears = time.ticks_ms(); global weight; weight = 0.0

    while True:
        if conn is None:
            try:
                c, addr = load_srv.accept()
                c.setblocking(False)
                conn = c
                print('Loadcell connected', addr)
                # Optimized tare: send multiple TARE commands for more accurate zero
                for _ in range(5):
                    try:
                        conn.send(b'TARE')
                    except:
                        pass
                    time.sleep(0.1)
            except OSError:
                pass
        try:
            client, _ = web.accept()
            handle_http(client, gate, sr, sl, conn, tz)
        except: pass
        if conn:
            try:
                data = conn.recv(64)
                if data:
                    for line in data.decode().splitlines(): weight = float(line)
                    print("Received weight:", weight)

            except: pass
        if state['opened'] and weight >= WEIGHT_THRESHOLD:
            close_gate(gate); state['opened'] = False; animate_ears_close(sr, sl)
        now = time.localtime(time.time() + tz); day = now[2]; stamp = f"{now[3]:02d}:{now[4]:02d}"
        if stamp in feed_times and last_trigger_day.get(stamp) != day:
            open_gate(gate); state['opened'] = True; last_trigger_day[stamp] = day; animate_ears_open(sr, sl)
        time.sleep(0.05)

if __name__ == '__main__':
   main()

