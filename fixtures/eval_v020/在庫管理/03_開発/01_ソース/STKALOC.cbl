       IDENTIFICATION DIVISION.
       PROGRAM-ID. STKALOC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY STK-CPY.
       01  WS-REQ-CASES        PIC 9(4).
       01  WS-REQ-QTY          PIC 9(6).
       PROCEDURE DIVISION.
           COMPUTE WS-REQ-QTY = WS-REQ-CASES * STK-CASE-SIZE.
           IF STK-QTY < WS-REQ-QTY
               CALL 'STKHOLD'
           END-IF.
           GOBACK.
