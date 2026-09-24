Namespace Acme.Api

    Public Class ApiClient

        Public Sub Connect()
            Dim svc = New API()
        End Sub

        Public Sub Load()
            Dim ord = New Acme.Order.OrderService()
        End Sub

    End Class

End Namespace
