       IDENTIFICATION DIVISION.
       PROGRAM-ID. FRTCALC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY FRT-CPY.
       01  WS-ISLAND-FLG       PIC X.
       01  WS-COLD-FLG         PIC X.
       PROCEDURE DIVISION.
           IF FRT-WEIGHT-KG > FRT-L-LIMIT
               CALL 'FRTQUOTE'
           ELSE
               CALL 'FRTBASE'
               IF WS-ISLAND-FLG = 'Y'
                   ADD FRT-ISLAND-FEE TO FRT-TOTAL
               END-IF
               IF WS-COLD-FLG = 'Y'
                   ADD FRT-COLD-FEE TO FRT-TOTAL
               END-IF
           END-IF.
           GOBACK.
