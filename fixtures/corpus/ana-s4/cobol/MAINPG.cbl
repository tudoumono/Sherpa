       IDENTIFICATION DIVISION.
       PROGRAM-ID. MAINPG.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY OUTER.
       PROCEDURE DIVISION.
           DISPLAY "CALL 'GHOST'". CALL 'REALPG'.
           EXEC SQL
               DECLARE C1 CURSOR FOR
               SELECT ID FROM CARD
           END-EXEC.
           EXEC SQL
               SELECT EXTRACT(YEAR FROM CREATED_AT) INTO :WS-Y
                 FROM CUSTOMER
           END-EXEC.
           EXEC SQL
               DECLARE C2 SCROLL CURSOR FOR STMT1
           END-EXEC.
           GOBACK.
