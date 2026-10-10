#include <Arduino.h>

// ============================================================
// UART 1: XIAO (receive only: nothing is ever written to it)
// ============================================================

HardwareSerial XIAO(1);

#define XIAO_RX 16
#define XIAO_TX 17
#define XIAO_BAUD 1200      // must match serial.baud on the radio


// ============================================================
// UART 2: MAX3232 -> printer (transmit only: nothing is ever read)
// ============================================================

HardwareSerial MAXUART(2);

#define MAX_RX 25
#define MAX_TX 26
#define MAX_BAUD 9600


// ============================================================
// LED
// ============================================================

#define LED_PIN 2
#define LED_IDLE_INTERVAL 500
#define LED_MESSAGE_TIME  5000


// ============================================================
// Printing
// ============================================================

#define FEED_LINES_BEFORE_CUT 5      // ESC d feed after the text
#define EXTRA_LFS_AFTER_MSG   3      // plain LFs sent right after the message
#define IDLE_FLUSH_MS         1000   // flush a message with no LF after this much silence

String xiaoMessage = "";
unsigned long lastXiaoByte = 0;

unsigned long lastBlink = 0;
bool ledState = false;
bool messageActive = false;
unsigned long messageTime = 0;


// ============================================================
// Printer
// ============================================================

void printMessage(const String &msg)
{
    // Everything below goes out as one continuous burst, with no delays

    // Message + LF prints immediately
    MAXUART.print(msg);
    MAXUART.write(0x0A);

    // Extra blank lines after the message
    for (int i = 0; i < EXTRA_LFS_AFTER_MSG; i++)
        MAXUART.write(0x0A);

    // Feed, then partial cut
    MAXUART.write(0x1B); MAXUART.write('d'); MAXUART.write((uint8_t)FEED_LINES_BEFORE_CUT);
    MAXUART.write(0x1D); MAXUART.write('V'); MAXUART.write((uint8_t)1);

    MAXUART.flush();
}


void finishMessage()
{
    xiaoMessage.trim();

    if (xiaoMessage.length() > 0)
    {
        Serial.print("XIAO -> printer: [");
        Serial.print(xiaoMessage);
        Serial.println("]");

        printMessage(xiaoMessage);

        messageActive = true;
        messageTime = millis();
        digitalWrite(LED_PIN, HIGH);
    }

    xiaoMessage = "";
}


// ============================================================
// Setup
// ============================================================

void setup()
{
    Serial.begin(115200);
    delay(500);

    XIAO.begin(XIAO_BAUD, SERIAL_8N1, XIAO_RX, XIAO_TX);
    MAXUART.begin(MAX_BAUD, SERIAL_8N1, MAX_RX, MAX_TX);

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    Serial.println("ESP32 XIAO -> printer bridge ready (one-way)");
}


// ============================================================
// XIAO input
// ============================================================

void processXiao()
{
    while (XIAO.available())
    {
        uint8_t c = XIAO.read();
        lastXiaoByte = millis();

        if (c == '\n')
            finishMessage();
        else if (c >= 0x20 && c <= 0x7E)      // printable ASCII only
            xiaoMessage += (char)c;
        // CR and everything else is dropped
    }

    // Fallback: if no LF arrives, flush after a quiet period
    if (xiaoMessage.length() > 0 && millis() - lastXiaoByte >= IDLE_FLUSH_MS)
        finishMessage();
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
// Main loop
// ============================================================

void loop()
{
    processXiao();
    processLED();
}