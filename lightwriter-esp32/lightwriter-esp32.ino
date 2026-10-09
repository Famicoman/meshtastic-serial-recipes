#include <Arduino.h>

// ============================================================
// UART 1: XIAO
// ============================================================

HardwareSerial XIAO(1);

#define XIAO_RX 16
#define XIAO_TX 17

#define XIAO_BAUD 1200


// ============================================================
// UART 2: MAX3232
// ============================================================

HardwareSerial MAXUART(2);

#define MAX_RX 25
#define MAX_TX 26

#define MAX_BAUD 1200


// ============================================================
// LED
// ============================================================

#define LED_PIN 2

#define LED_IDLE_INTERVAL 500
#define LED_RX_INTERVAL   167
#define LED_MESSAGE_TIME  5000


// ============================================================
// XIAO -> MAX3232
// ============================================================

String xiaoMessage = "";


// ============================================================
// MAX3232 -> XIAO
// ============================================================

String maxMessage = "";

unsigned long maxLastCharTime = 0;

bool maxBuffering = false;

#define MAX_BUFFER_TIMEOUT 5000


// ============================================================
// LED state
// ============================================================

unsigned long lastBlink = 0;

bool ledState = false;

bool xiaoMessageActive = false;
unsigned long xiaoMessageTime = 0;


// ============================================================
// Setup
// ============================================================

void setup()
{
    Serial.begin(115200);

    delay(500);


    // --------------------------------------------------------
    // XIAO UART
    // --------------------------------------------------------

    XIAO.begin(
        XIAO_BAUD,
        SERIAL_8N1,
        XIAO_RX,
        XIAO_TX
    );


    // --------------------------------------------------------
    // MAX3232 UART
    // --------------------------------------------------------

    MAXUART.begin(
        MAX_BAUD,
        SERIAL_8N1,
        MAX_RX,
        MAX_TX
    );


    // --------------------------------------------------------
    // LED
    // --------------------------------------------------------

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);


    // --------------------------------------------------------
    // Startup
    // --------------------------------------------------------

    Serial.println();
    Serial.println("==========================================");
    Serial.println(" ESP32 SERIAL BRIDGE");
    Serial.println("==========================================");
    Serial.println("XIAO UART:    GPIO16 RX / GPIO17 TX");
    Serial.println("MAX3232 UART: GPIO25 RX / GPIO26 TX");
    Serial.println("Baud:         1200 8N1");
    Serial.println("MAX->XIAO:    5 second inactivity buffer");
    Serial.println("MAX->XIAO:    Printable ASCII only");
    Serial.println("LED:          3x RX / 5s XIAO activity");
    Serial.println("==========================================");
}


// ============================================================
// XIAO -> MAX3232
// ============================================================

void processXiao()
{
    while (XIAO.available())
    {
        char c = XIAO.read();


        // ----------------------------------------------------
        // LF terminates the message
        // ----------------------------------------------------

        if (c == '\n')
        {
            xiaoMessage.trim();

            if (xiaoMessage.length() > 0)
            {
                String output = xiaoMessage + " ";


                Serial.print("XIAO -> MAX3232 Received:   [");
                Serial.print(xiaoMessage);
                Serial.println("]");


                Serial.print("XIAO -> MAX3232 Forwarding: [");
                Serial.print(output);
                Serial.println("]");


                // Send message
                MAXUART.print(output);
                MAXUART.print("\r\n");


                // ------------------------------------------------
                // Solid LED for 5 seconds
                // ------------------------------------------------

                xiaoMessageActive = true;
                xiaoMessageTime = millis();

                digitalWrite(LED_PIN, HIGH);
            }

            xiaoMessage = "";
        }


        // ----------------------------------------------------
        // Ignore CR
        // ----------------------------------------------------

        else if (c != '\r')
        {
            xiaoMessage += c;
        }
    }
}


// ============================================================
// MAX3232 -> XIAO
// ============================================================

void processMax()
{
    while (MAXUART.available())
    {
        uint8_t c = MAXUART.read();


        // ----------------------------------------------------
        // DEBUG: print every received byte
        // ----------------------------------------------------

        Serial.print("MAX3232 RX byte: 0x");

        if (c < 0x10)
            Serial.print("0");

        Serial.print(c, HEX);


        // ----------------------------------------------------
        // Printable ASCII
        // ----------------------------------------------------

        if (c >= 0x20 && c <= 0x7E)
        {
            Serial.print(" [");
            Serial.print((char)c);
            Serial.println("]");
        }


        // ----------------------------------------------------
        // CR
        // ----------------------------------------------------

        else if (c == '\r')
        {
            Serial.println(" [CR]");
        }


        // ----------------------------------------------------
        // LF
        // ----------------------------------------------------

        else if (c == '\n')
        {
            Serial.println(" [LF]");
        }


        // ----------------------------------------------------
        // Everything else is non-printable
        // ----------------------------------------------------

        else
        {
            Serial.println(" [NON-PRINTABLE - DISCARDED]");
        }


        // ----------------------------------------------------
        // Start/continue 5-second receive window
        // ----------------------------------------------------

        maxBuffering = true;
        maxLastCharTime = millis();


        // ----------------------------------------------------
        // CR/LF are framing characters, not message data
        // ----------------------------------------------------

        if (c == '\r' || c == '\n')
        {
            continue;
        }


        // ----------------------------------------------------
        // ONLY printable ASCII gets into maxMessage
        // ----------------------------------------------------

        if (c >= 0x20 && c <= 0x7E)
        {
            maxMessage += (char)c;
        }
    }


    // --------------------------------------------------------
    // 5 seconds of inactivity?
    // --------------------------------------------------------

    if (maxBuffering)
    {
        if (millis() - maxLastCharTime >= MAX_BUFFER_TIMEOUT)
        {
            if (maxMessage.length() > 0)
            {
                Serial.print("MAX3232 -> XIAO Sending: [");
                Serial.print(maxMessage);
                Serial.println("]");


                XIAO.print(maxMessage);
                XIAO.print("\r\n");
            }
            else
            {
                Serial.println("MAX3232 -> XIAO: No printable data to send.");
            }


            maxMessage = "";

            maxBuffering = false;
        }
    }
}


// ============================================================
// LED
// ============================================================

void processLED()
{
    unsigned long now = millis();


    // --------------------------------------------------------
    // XIAO -> MAX3232 message
    //
    // Solid for 5 seconds
    // --------------------------------------------------------

    if (xiaoMessageActive)
    {
        if (now - xiaoMessageTime >= LED_MESSAGE_TIME)
        {
            xiaoMessageActive = false;

            digitalWrite(LED_PIN, LOW);

            lastBlink = now;
            ledState = false;
        }

        return;
    }


    // --------------------------------------------------------
    // MAX3232 -> XIAO buffering
    //
    // Blink 3x faster than idle
    // --------------------------------------------------------

    if (maxBuffering)
    {
        if (now - lastBlink >= LED_RX_INTERVAL)
        {
            lastBlink = now;

            ledState = !ledState;

            digitalWrite(LED_PIN, ledState);
        }

        return;
    }


    // --------------------------------------------------------
    // Idle
    // --------------------------------------------------------

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

    processMax();

    processLED();
}