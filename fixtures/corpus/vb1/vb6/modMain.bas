Attribute VB_Name = "modMain"

Sub Main()
    LogMessage "starting"
    Call LoadOrders
    Dim conn As Object
    Set conn = CreateObject("ADODB.Connection")
End Sub

Private Sub LogMessage(msg As String)
End Sub
