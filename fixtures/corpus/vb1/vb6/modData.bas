Attribute VB_Name = "modData"

Function LoadOrders(id As Long) As Object
    Dim sql As String
    sql = "SELECT * " & _
          "FROM ORDERS WHERE ID = " & id
    Dim rs As Object
    Set rs = CreateObject("ADODB.Recordset")
    LoadOrders = rs
End Function
