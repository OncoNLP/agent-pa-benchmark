import os
import json
from dotenv import load_dotenv
from google import genai
from google.genai import types

# 1. Test Env Loading
load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

from google import genai

client = genai.Client()

print("List of models that support generateContent:\n")
for m in client.models.list():
    for action in m.supported_actions:
        if action == "generateContent":
            print(m.name)

print("List of models that support embedContent:\n")
for m in client.models.list():
    for action in m.supported_actions:
        if action == "embedContent":
            print(m.name)