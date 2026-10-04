       IDENTIFICATION DIVISION.
       PROGRAM-ID. FOURPG.
       PROCEDURE DIVISION.
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM A.B.C.D
           END-EXEC.
           GOBACK.
