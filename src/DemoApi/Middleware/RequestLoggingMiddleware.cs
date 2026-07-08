using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Serialization;
using DemoApi.Logging;
using DemoApi.Messaging;

namespace DemoApi.Middleware;

public class RequestLoggingMiddleware
{
    private const int SimulatedLatencyMs = 2000;

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    private readonly RequestDelegate _next;
    private readonly IRequestLogPublisher _publisher;

    public RequestLoggingMiddleware(RequestDelegate next, IRequestLogPublisher publisher)
    {
        _next = next;
        _publisher = publisher;
    }

    public async Task InvokeAsync(HttpContext context)
    {
        // /health is a Docker/orchestration probe, not application traffic - skip
        // structured logging and RabbitMQ publishing entirely so frequent polling
        // (every few seconds, for the container's lifetime) never inflates
        // logs_raw/logs_agg with a synthetic endpoint or trips anomaly detection.
        if (context.Request.Path.Equals("/health", StringComparison.OrdinalIgnoreCase))
        {
            await _next(context);
            return;
        }

        var requestId = Guid.NewGuid().ToString();

        int? simulatedLatencyMs = null;
        if (context.Request.Headers.TryGetValue("X-Simulate-Latency", out var headerValue) &&
            string.Equals(headerValue.ToString(), "true", StringComparison.OrdinalIgnoreCase))
        {
            simulatedLatencyMs = SimulatedLatencyMs;
        }

        var stopwatch = Stopwatch.StartNew();

        if (simulatedLatencyMs.HasValue)
        {
            await Task.Delay(simulatedLatencyMs.Value);
        }

        await _next(context);

        stopwatch.Stop();

        var logEntry = new RequestLogEntry
        {
            Timestamp = DateTime.UtcNow,
            Endpoint = context.Request.Path.Value ?? string.Empty,
            Method = context.Request.Method,
            StatusCode = context.Response.StatusCode,
            UserId = context.Items.TryGetValue("UserId", out var userId) ? userId as string : null,
            ResponseTimeMs = stopwatch.ElapsedMilliseconds,
            RequestId = requestId,
            SimulatedLatencyMs = simulatedLatencyMs
        };

        Console.WriteLine(JsonSerializer.Serialize(logEntry, JsonOptions));

        await _publisher.PublishAsync(logEntry);
    }
}
