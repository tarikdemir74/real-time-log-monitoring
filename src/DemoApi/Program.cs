using DemoApi.Messaging;
using DemoApi.Middleware;

var builder = WebApplication.CreateBuilder(args);

// Add services to the container.

builder.Services.AddControllers();
// Learn more about configuring OpenAPI at https://aka.ms/aspnet/openapi
builder.Services.AddOpenApi();

builder.Services.Configure<RabbitMqOptions>(builder.Configuration.GetSection("RabbitMq"));
builder.Services.AddSingleton<IRequestLogPublisher, RabbitMqRequestLogPublisher>();

var app = builder.Build();

// Configure the HTTP request pipeline.
if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
}

app.UseMiddleware<RequestLoggingMiddleware>();

app.UseAuthorization();

// Lightweight liveness/readiness probe for Docker's healthcheck and for
// TrafficSimulator's own startup wait. Deliberately bypassed by
// RequestLoggingMiddleware (see the middleware's early-return check) so
// repeated health polling never pollutes logs_raw/logs_agg or trips
// anomaly detection.
app.MapGet("/health", () => Results.Ok(new { status = "healthy" }));

app.MapControllers();

app.Run();
