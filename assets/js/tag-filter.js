/* Tag filtering for blog-home.html ----------------------------------------- */
document.addEventListener('DOMContentLoaded', () => {
  const tagBtns = document.querySelectorAll('.tag-btn');
  const cards   = document.querySelectorAll('#posts-grid [data-tags]');

  tagBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const tag = btn.dataset.tag;

      tagBtns.forEach(b => {
        b.classList.toggle('btn-primary', b.dataset.tag === tag);
        b.classList.toggle('btn-outline', b.dataset.tag !== tag);
      });

      cards.forEach(card => {
        const cardTags = card.dataset.tags ? card.dataset.tags.split(',') : [];
        card.style.display = (tag === 'all' || cardTags.includes(tag)) ? '' : 'none';
      });
    });
  });
});
