"""Generate original, synthetic test images. No user files or external media are used."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent / "fixtures"


def font(size):
    return ImageFont.load_default(size=size)


def main():
    ROOT.mkdir(exist_ok=True)
    image = Image.new("RGB", (800, 500), "#f3f5f7")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 140, 500), fill="#25334a")
    draw.text((20, 24), "SALES", font=font(23), fill="white")
    draw.text((166, 26), "Pipeline", font=font(28), fill="#182435")
    for x, label in [(166, "Leads"), (376, "Proposal"), (586, "Won")]:
        draw.text((x, 95), label, font=font(22), fill="#182435")
        for y in (142, 240):
            draw.rounded_rectangle((x, y, x+185, y+78), radius=8, fill="white")
            draw.text((x+13, y+15), "Client " + str(x+y), font=font(16), fill="#182435")
            draw.text((x+13, y+43), "$ 4,200", font=font(16), fill="#657286")
    image.save(ROOT / "001.png")

    image = Image.new("RGB", (800, 500), "#111111")
    draw = ImageDraw.Draw(image)
    draw.text((65, 165), "NOVA", font=font(105), fill="white")
    draw.text((72, 340), "Everyday essentials", font=font(23), fill="white")
    image.save(ROOT / "002.png")

    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((280, 90, 520, 435), radius=24, fill="#151515")
    draw.rectangle((330, 45, 470, 100), fill="#151515")
    draw.text((320, 210), "LUME", font=font(44), fill="#daac53")
    draw.text((317, 277), "BODY LOTION", font=font(16), fill="#daac53")
    image.save(ROOT / "003.png")

    image = Image.new("RGB", (800, 500), "#ffef00")
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, 784, 484), outline="black", width=12)
    draw.text((35, 30), "RAW", font=font(145), fill="black")
    draw.text((45, 209), "DESIGN", font=font(108), fill="black")
    draw.rectangle((40, 352, 760, 449), outline="black", width=8)
    draw.text((60, 373), "NO RULES / 2026", font=font(44), fill="black")
    image.save(ROOT / "004.png")


if __name__ == "__main__":
    main()
