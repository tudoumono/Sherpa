       IDENTIFICATION DIVISION.
       PROGRAM-ID. MIXPG.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM M
           END-EXEC.
           GOBACK.
