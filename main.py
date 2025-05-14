from machine import Pin
from hx711 import HX711
import time
import socket
import network

print('running boot')

# WIFI
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
password = 'UYyeFrWyEct3'

if wlan.isconnected():
    wlan.disconnect()
    time.sleep(1)

if not wlan.isconnected():
    print('connecting to network...')
    wlan.connect('The Standard', password)
    tries = 0
    while not wlan.isconnected() and tries < 30:
        time.sleep(2)
        tries += 1

    if wlan.isconnected():
        print("WiFi connected:", wlan.ifconfig())
    else:
        print("Mission failed")

# HX711
print("Waiting for HX711 to power up...")
time.sleep(2)

data_pin = Pin(21, Pin.IN)
clock_pin = Pin(22, Pin.OUT)

try:
    hx = HX711(clock_pin, data_pin)
    hx.set_scale(286)
    for _ in range(3):
        hx.tare()
        time.sleep(0.5)
    print("Tare complete.")
except Exception as e:
    print("HX711 error:", e)
    raise


receiver_ip = '10.3.2.251'
port = 12345

def connect_socket():
    while True:
        try:
            s = socket.socket()
            s.connect((receiver_ip, port))
            print("Connected to receiver.")
            time.sleep(1) 
            return s
        except Exception as e:
            print("Connection failed, retrying:", e)
            time.sleep(2)

sock = connect_socket()


alpha = 0.15
ema_weight = 0
stable_threshold = 1.0
stable_duration = 60
stable_count = 0


while True:
    try:
        
        new_weight = hx.get_units()
        ema_weight = alpha * new_weight + (1 - alpha) * ema_weight
        print("Weight (EMA): {:.2f} g".format(ema_weight))

        if abs(ema_weight) < stable_threshold:
            stable_count += 1
        else:
            stable_count = 0

        if stable_count >= stable_duration:
            print("Auto-retaring...")
            hx.tare()
            stable_count = 0

        msg = "{:.2f}\n".format(ema_weight)
        try:
            sock.sendall(msg.encode())
        except OSError as e:
            print("Send failed:", e)
            try:
                sock.close()
            except:
                pass
            print("Reconnecting...")
            sock = connect_socket()

    except OSError as e:
        print("HX711 read error:", e)

    time.sleep(1)