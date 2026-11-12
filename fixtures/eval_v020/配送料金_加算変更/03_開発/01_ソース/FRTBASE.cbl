       IDENTIFICATION DIVISION.
       PROGRAM-ID. FRTBASE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY FRT-CPY.
       PROCEDURE DIVISION.
           EVALUATE TRUE
               WHEN FRT-WEIGHT-KG <= 2
                   MOVE 800 TO FRT-TOTAL
               WHEN FRT-WEIGHT-KG <= 10
                   MOVE 1200 TO FRT-TOTAL
               WHEN FRT-WEIGHT-KG <= 20
                   MOVE 1800 TO FRT-TOTAL
           END-EVALUATE.
           GOBACK.
