namespace DemoApi.Models;

public record Product(int Id, string Name, decimal Price);

public static class ProductCatalog
{
    public static readonly List<Product> Items = new()
    {
        new Product(1, "Wireless Mouse", 19.99m),
        new Product(2, "Mechanical Keyboard", 49.99m),
        new Product(3, "USB-C Hub", 29.99m),
        new Product(4, "27-inch Monitor", 199.99m),
        new Product(5, "Laptop Stand", 24.99m)
    };
}
