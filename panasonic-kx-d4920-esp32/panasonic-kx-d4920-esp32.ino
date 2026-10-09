#include <Arduino.h>

// ============================================================
// UART 1: XIAO
// ============================================================

HardwareSerial XIAO(1);

#define XIAO_RX 16
#define XIAO_TX 17

#define XIAO_BAUD 1200


// ============================================================
// UART 2: MAX3232 -> Panasonic KX-D4920
// ============================================================

HardwareSerial MAXUART(2);

#define MAX_RX 25
#define MAX_TX 26

#define MAX_BAUD   1200
#define MAX_CONFIG SERIAL_8N1


// ============================================================
// Timing and terminal output
// ============================================================

#define PREAMBLE_CRS          8
#define IDLE_FLUSH_MS      1000  // XIAO -> terminal
#define MAX_TO_XIAO_TIMEOUT 5000 // MAX3232 -> XIAO


// ============================================================
// LED
// ============================================================

#define LED_PIN 2

#define LED_IDLE_INTERVAL 500
#define LED_MESSAGE_TIME  5000


// ============================================================
// XIAO -> MAX3232 state
// ============================================================

String xiaoMessage = "";
unsigned long lastXiaoByte = 0;


// ============================================================
// MAX3232 -> XIAO state
// ============================================================

String maxMessage = "";
unsigned long lastMaxByte = 0;


// ============================================================
// LED state
// ============================================================

unsigned long lastBlink = 0;
bool ledState = false;

bool messageActive = false;
unsigned long messageTime = 0;


// ============================================================
// LED message indication
// ============================================================

void indicateMessage()
{
    messageActive = true;
    messageTime = millis();
    digitalWrite(LED_PIN, HIGH);
}


// ============================================================
// XIAO -> terminal
// ============================================================

void sendToTerminal(const String &s)
{
    // Sacrificial carriage returns
    for (int i = 0; i < PREAMBLE_CRS; i++)
        MAXUART.write('\r');

    for (size_t i = 0; i < s.length(); i++)
    {
        char c = s[i];

        // Printable ASCII only
        if (c < 0x20 || c > 0x7E)
            continue;

        MAXUART.write((uint8_t)c);
    }

    MAXUART.write('\r');
    MAXUART.write('\n');
    MAXUART.flush();
}


void finishXiaoMessage()
{
    xiaoMessage.trim();

    if (xiaoMessage.length() > 0)
    {
        Serial.print("XIAO -> terminal: [");
        Serial.print(xiaoMessage);
        Serial.println("]");

        sendToTerminal(xiaoMessage);
        indicateMessage();
    }

    xiaoMessage = "";
}


void processXiao()
{
    while (XIAO.available())
    {
        uint8_t c = XIAO.read();
        lastXiaoByte = millis();

        if (c == '\n')
        {
            finishXiaoMessage();
        }
        else if (c >= 0x20 && c <= 0x7E)
        {
            xiaoMessage += (char)c;
        }

        // CR, control characters, and other non-printable
        // bytes are discarded.
    }

    // Fallback for messages without a terminating LF
    if (xiaoMessage.length() > 0 &&
        millis() - lastXiaoByte >= IDLE_FLUSH_MS)
    {
        finishXiaoMessage();
    }
}


// ============================================================
// MAX3232 -> XIAO
// ============================================================

void sendToXiao(const String &s)
{
    if (s.length() == 0)
        return;

    Serial.print("MAX3232 -> XIAO: [");
    Serial.print(s);
    Serial.println("]");

    // Send only printable ASCII.
    // No CR/LF is added automatically.
    for (size_t i = 0; i < s.length(); i++)
    {
        uint8_t c = (uint8_t)s[i];

        if (c >= 0x20 && c <= 0x7E)
            XIAO.write(c);
    }

    XIAO.flush();

    indicateMessage();
}


void finishMaxMessage()
{
    if (maxMessage.length() > 0)
        sendToXiao(maxMessage);

    maxMessage = "";
}


void processMaxInput()
{
    while (MAXUART.available())
    {
        uint8_t c = MAXUART.read();

        // Only printable ASCII is allowed into the buffer.
        // Non-printable bytes are ignored.
        if (c >= 0x20 && c <= 0x7E)
        {
            maxMessage += (char)c;

            // Restart the timeout on every accepted byte.
            lastMaxByte = millis();

            Serial.print("MAX3232 RX: ");
            Serial.write(c);
            Serial.println();
        }
        else
        {
            Serial.print("MAX3232 ignored byte: 0x");

            if (c < 0x10)
                Serial.print('0');

            Serial.println(c, HEX);
        }
    }

    // Forward the complete buffered input only after
    // 5 seconds without another printable character.
    if (maxMessage.length() > 0 &&
        millis() - lastMaxByte >= MAX_TO_XIAO_TIMEOUT)
    {
        finishMaxMessage();
    }
}


// ============================================================
// LED
// ============================================================

void processLED()
{
    unsigned long now = millis();

    if (messageActive)
    {
        if (now - messageTime >= LED_MESSAGE_TIME)
        {
            messageActive = false;
            digitalWrite(LED_PIN, LOW);

            lastBlink = now;
            ledState = false;
        }

        return;
    }

    if (now - lastBlink >= LED_IDLE_INTERVAL)
    {
        lastBlink = now;
        ledState = !ledState;
        digitalWrite(LED_PIN, ledState);
    }
}


// ============================================================
// Setup
// ============================================================

void setup()
{
    Serial.begin(115200);
    delay(500);

    // Increase UART receive buffers
    XIAO.setRxBufferSize(1024);
    MAXUART.setRxBufferSize(1024);

    XIAO.begin(
        XIAO_BAUD,
        SERIAL_8N1,
        XIAO_RX,
        XIAO_TX
    );

    MAXUART.begin(
        MAX_BAUD,
        MAX_CONFIG,
        MAX_RX,
        MAX_TX
    );

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    Serial.println();
    Serial.println("==========================================");
    Serial.println(" ESP32 Bidirectional Serial Bridge");
    Serial.println("==========================================");
    Serial.println("XIAO UART:    GPIO16 RX, GPIO17 TX");
    Serial.println("              1200 baud, 8N1");
    Serial.println("MAX3232 UART: GPIO25 RX, GPIO26 TX");
    Serial.println("              1200 baud, 8N1");
    Serial.println("XIAO -> terminal: LF or 1s idle timeout");
    Serial.println("MAX3232 -> XIAO:   5s idle timeout");
    Serial.println("Non-printable input bytes are discarded");
    Serial.println("==========================================");
}


// ============================================================
// Main loop
// ============================================================

void loop()
{
    processXiao();
    processMaxInput();
    processLED();
}