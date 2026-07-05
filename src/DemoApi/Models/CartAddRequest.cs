namespace DemoApi.Models;

public class CartAddRequest
{
    public string? UserId { get; set; }
    public int ProductId { get; set; }
    public int Quantity { get; set; }
}
