namespace DemoApi.Models;

public class CheckoutRequest
{
    public string? UserId { get; set; }
    public decimal Amount { get; set; }
}
