       IDENTIFICATION DIVISION.
       PROGRAM-ID. ZAIHIKI.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY ZAI-CPY.
       01  WS-REQ-CASES        PIC 9(4).
       01  WS-REQ-QTY          PIC 9(6).
       PROCEDURE DIVISION.
           COMPUTE WS-REQ-QTY = WS-REQ-CASES * ZAI-CASE-SIZE.
           IF ZAI-QTY < WS-REQ-QTY
               CALL 'ZAIHORYU'
           END-IF.
           GOBACK.
