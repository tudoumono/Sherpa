       IDENTIFICATION DIVISION.
       PROGRAM-ID. THREEPG.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM DB1.S3.TT
           END-EXEC.
           GOBACK.
