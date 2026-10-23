       IDENTIFICATION DIVISION.
       PROGRAM-ID. PTADD.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY PT-CPY.
       01  WS-RANK             PIC X.
       01  WS-SALES-AMT        PIC 9(9).
       PROCEDURE DIVISION.
           EVALUATE WS-RANK
               WHEN 'N'
                   COMPUTE PT-POINT = WS-SALES-AMT * PT-RATE-NORMAL
               WHEN 'S'
                   COMPUTE PT-POINT = WS-SALES-AMT * PT-RATE-SILVER
               WHEN 'G'
                   COMPUTE PT-POINT = WS-SALES-AMT * PT-RATE-GOLD
           END-EVALUATE.
           GOBACK.
