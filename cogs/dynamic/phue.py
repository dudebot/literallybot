# should be able to interact with the available devices (if owner) and assign function command to listen to create alarm or something specific like remote mood lighting

# from phue import Bridge
# import random
# import time

# # Replace 'BRIDGE_IP' with your actual Hue Bridge IP address
# bridge_ip = '192.168.0.239'
# bridge = Bridge(bridge_ip)

# # Connect to the bridge
# bridge.connect()
# # Get the lights
# lights = bridge.lights

# # Function to flash lights
# def flash_lights(flash_times, delay):
#     for light in lights:
#         light.on = True
#     for _ in range(flash_times):
#         for light in lights:
#             if "bed" in light.name.lower():
#                 random_hue = random.randint(0, 65535)
#                 random_sat = random.randint(100, 254)
#                 light.on = not light.on
#                 if light.on:
#                     light.brightness = 254
#                     light.hue = random_hue
#                     light.saturation = random_sat
#         time.sleep(delay)

# # Flash the lights 10 times with 0.1 second delay
# flash_lights(flash_times=20, delay=2)

# # lights[0].on=True
# # lights[0].brightness=254
# # lights[0].hue=50000
# # lights[0].saturation=254