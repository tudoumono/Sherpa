namespace Acme.Order;
using Acme.Core;
using Acme.Data;

public class OrderService : BaseService, IOrderService
{
    private IOrderRepo _repo;

    public OrderService()
    {
        _repo = new OrderRepo();
    }
}
