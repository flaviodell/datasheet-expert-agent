# Stress Test — STM32F103xB Datasheet

Tested on: STM32F103xB datasheet (DS5319 Rev 20, 115 pages)
Model: llama-3.1-8b-instant via Groq
Embedding: intfloat/multilingual-e5-small

---

| # | Query | Expected answer | Result |
|---|---|---|---|
| 1 | Flash memory size? | 64 or 128 KB | ✅ |
| 2 | Maximum CPU frequency? | 72 MHz | ✅ |
| 3 | SPI interfaces on LQFP48? | 2 (SPI1, SPI2) | ✅ |
| 4 | Typical current at 72 MHz, all peripherals enabled, code from Flash? | 36 mA (Table 17, p.47) | ✅ |
| 5 | Max current at 72 MHz, peripherals disabled, code from RAM? | 29.5 mA at TA=105°C (Table 14, p.41) | ✅ |
| 6 | Minimum operating VDD? | 2.0 V (Table 9) — not -0.3 V which is AMR | ✅ |
| 7 | Absolute max VDD vs recommended operating voltage? | AMR: 4.0 V / Operating: 2.0–3.6 V | ✅ |
| 8 | Difference between 'max' and 'typ' current values? | max = guaranteed worst case; typ = measured at 25°C/3.3V, not guaranteed | ✅ |
| 9 | GPIO count for STM32F103Rx with 128 KB Flash (Table 2)? | 51 | ✅ |
| 10 | Minimum ADC sampling time for VREFINT? | 17.1 µs (Table 12, p.39) | ✅ |
| 11 | Communication interfaces listed on page 1 Features? | I²C ×2, USART ×3, SPI ×2, CAN, USB 2.0 | ⚠️ partial |

**Result: 10/11 correct, 1 partial.**
Query 11 retrieved pinout page chunks alongside the Features anchor,
causing the model to mix interface names with pin labels.