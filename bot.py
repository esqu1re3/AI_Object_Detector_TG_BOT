import io
import cv2
import yaml
import joblib
import logging
import torch
import numpy as np
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageStat
from torchvision import transforms
import telebot
import pandas as pd
from src.models_loader import initialize_models
from src.utils import setup_logging, catch_exceptions

#setting base directory 
BASE_DIR = Path(__file__).resolve().parent

setup_logging()

settings_path = BASE_DIR / "config" / "settings.yaml"
print(f"Loading settings from: {settings_path}")
settings = yaml.safe_load(open(settings_path, "r"))
TOKEN = settings.get("telegram_bot_token")
if not TOKEN:
    raise ValueError(f"telegram_bot_token is not set in {settings_path}")

selector_path = BASE_DIR / "models" / "model_selector_dt.joblib"
config_path = BASE_DIR / "config" / "models_config.json"

print(f"Loading selector from: {selector_path}")
print(f"Loading model config from: {config_path}")

selector = joblib.load(selector_path)
models_available = initialize_models(str(config_path))
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
for info in models_available.values():
    info["model"].to(device).eval()

transform = transforms.Compose([transforms.ToTensor()])
bot = telebot.TeleBot(TOKEN)

#extracting image features using a little bit of randomness to ensure diversity in model selection
def extract_image_features(image: Image.Image) -> list:
    
    w, h = image.size
    ratio_hw = h / w
    
    img_array = np.array(image)
    
    img_hash = np.sum(img_array) % 1000 / 1000.0
    
    if len(img_array.shape) == 3:
        hsv = cv2.cvtColor(img_array, cv2.COLOR_RGB2HSV)
        
        saturation = np.mean(hsv[:,:,1]) / 255.0
        brightness = np.mean(hsv[:,:,2]) / 255.0
        
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    else:
        gray = img_array
        saturation = 0.0
        brightness = np.mean(gray) / 255.0
    
    sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    gradient_magnitude = np.sqrt(sobelx**2 + sobely**2)
    detail_level = np.mean(gradient_magnitude) / 255.0
    
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    min_area = (w * h) * 0.001
    significant_contours = [c for c in contours if cv2.contourArea(c) > min_area]
    
    num_objects = len(significant_contours)
    
    if num_objects > 0:
        areas = [cv2.contourArea(c) for c in significant_contours]
        mean_area = np.mean(areas) / (w * h)
    else:
        mean_area = 0.1
    
    num_objects_mod = max(1, int(num_objects * (0.8 + 0.4 * img_hash)))
    mean_area_mod = mean_area * (0.7 + 0.6 * img_hash)
    categories_mod = max(1, int(5 * saturation + 3 * detail_level + 2 * img_hash))
    
    return [
        num_objects_mod,           
        min(mean_area_mod, 1.0),    
        min(categories_mod, 10),    
        ratio_hw                   
    ]

#drawing boxes function
def draw_boxes(image: Image.Image, boxes, labels, scores, categories, threshold=0.5) -> Image.Image:
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("OCR-B.ttf", 16)
    except IOError:
        font = ImageFont.load_default()
    for box, lbl, score in zip(boxes, labels, scores):
        if score < threshold:
            continue
        x1, y1, x2, y2 = box.tolist()
        draw.rectangle([x1, y1, x2, y2], outline="lime", width=3)
        label = f"{categories[lbl]} {score*100:.0f}%"
        
        outline_color = "black"
        for dx, dy in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]:
            draw.text((x1 + 3 + dx, y1 - 3 + dy), label, font=font, fill=outline_color)
        
        draw.text((x1 + 3, y1 - 3), label, font=font, fill="white")
    return image

#telegram bot functions
@bot.message_handler(commands=["start"])
def handle_start(message):
    bot.send_message(
        message.chat.id,
        "Hello! I can detect objects on your photo using the best pretrained model picked by trained Decision Tree Classifier.\n"
        "Please send me a photo, and I will return the image with detected objects and tell you which model I have used."
    )

@bot.message_handler(content_types=["photo"])
@catch_exceptions
def handle_photo(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username or "unknown"
    logging.info(f"Photo received from user {user_id} (@{username}) at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    file_info = bot.get_file(message.photo[-1].file_id)
    img_bytes = bot.download_file(file_info.file_path)
    image = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    feats = extract_image_features(image)
    
    df_feats = pd.DataFrame([feats], columns=["num_objects", "mean_box_area", "num_categories", "ratio_hw"])
    chosen_key = selector.predict(df_feats)[0]
    model_info = models_available[chosen_key]
    model = model_info["model"]
    categories = model_info["categories"]

    tensor = transform(image).unsqueeze(0).to(device)
    with torch.no_grad():
        pred = model(tensor)[0]

    result_img = draw_boxes(image.copy(), pred["boxes"], pred["labels"], pred["scores"], categories)
    buf = io.BytesIO()
    result_img.save(buf, format="JPEG")
    buf.seek(0)

    bot.send_photo(
        chat_id,
        buf,
        caption=f"Model used: {model_info['name']}"
    )

if __name__ == '__main__':
    bot.polling()