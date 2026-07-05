using DemoApi.Models;
using Microsoft.AspNetCore.Mvc;

namespace DemoApi.Controllers;

[ApiController]
[Route("api/payment")]
public class PaymentController : ControllerBase
{
    [HttpPost("checkout")]
    public IActionResult Checkout([FromBody] CheckoutRequest? request)
    {
        if (!string.IsNullOrWhiteSpace(request?.UserId))
        {
            HttpContext.Items["UserId"] = request.UserId;
        }

        if (request is null || request.Amount <= 0)
        {
            return BadRequest(new { message = "Amount must be greater than zero." });
        }

        return Ok(new
        {
            transactionId = Guid.NewGuid().ToString(),
            status = "success",
            amount = request.Amount
        });
    }
}
