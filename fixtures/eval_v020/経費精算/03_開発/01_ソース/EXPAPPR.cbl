       IDENTIFICATION DIVISION.
       PROGRAM-ID. EXPAPPR.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY EXP-CPY.
       01  WS-APPROVER         PIC X(8).
       PROCEDURE DIVISION.
           EVALUATE TRUE
               WHEN EXP-AMOUNT >= EXP-LIMIT-DIR
                   MOVE 'DIRECTOR' TO WS-APPROVER
               WHEN EXP-AMOUNT >= EXP-LIMIT-MGR
                   MOVE 'DEPT-MGR' TO WS-APPROVER
               WHEN OTHER
                   MOVE 'SECT-MGR' TO WS-APPROVER
           END-EVALUATE.
           GOBACK.
