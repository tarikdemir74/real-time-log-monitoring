using DemoApi.Models;
using Microsoft.AspNetCore.Mvc;

namespace DemoApi.Controllers;

[ApiController]
[Route("api/cart")]
public class CartController : ControllerBase
{
    [HttpPost("add")]
    public IActionResult AddToCart([FromBody] CartAddRequest? request)
    {
        if (!string.IsNullOrWhiteSpace(request?.UserId))
        {
            HttpContext.Items["UserId"] = request.UserId;
        }

        if (request is null || request.Quantity <= 0)
        {
            return BadRequest(new { message = "Quantity must be greater than zero." });
        }

        var product = ProductCatalog.Items.FirstOrDefault(p => p.Id == request.ProductId);
        if (product is null)
        {
            return NotFound(new { message = $"Product {request.ProductId} not found." });
        }

        return Ok(new
        {
            message = "Item added to cart.",
            productId = product.Id,
            quantity = request.Quantity
        });
    }
}
