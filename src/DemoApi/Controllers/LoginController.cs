using DemoApi.Models;
using Microsoft.AspNetCore.Mvc;

namespace DemoApi.Controllers;

[ApiController]
[Route("api/login")]
public class LoginController : ControllerBase
{
    [HttpPost]
    public IActionResult Login([FromBody] LoginRequest? request)
    {
        if (string.IsNullOrWhiteSpace(request?.Username) || string.IsNullOrWhiteSpace(request?.Password))
        {
            return BadRequest(new { message = "Username and password are required." });
        }

        var userId = $"u-{request.Username}";
        HttpContext.Items["UserId"] = userId;

        return Ok(new LoginResponse
        {
            UserId = userId,
            Token = "fake-jwt-token"
        });
    }
}
