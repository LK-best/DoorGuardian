import qrcode

# Вставьте вашу ссылку на релиз GitHub
url = "https://github.com/LK-best/DoorGuardian/releases/tag/v0.2.0"

qr = qrcode.QRCode(
    version=1,
    error_correction=qrcode.constants.ERROR_CORRECT_H,
    box_size=10,
    border=4,
)
qr.add_data(url)
qr.make(fit=True)

img = qr.make_image(fill_color="#1E1E2E", back_color="white")
img.save("DoorGuardian_QR.png")

print("✅ QR-код успешно создан!")